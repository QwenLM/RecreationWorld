#!/usr/bin/env python3
"""Android pipeline for the deployment adapter in-pod release runtime.

deployment adapter enters ``core/pipeline.py`` first. This module launches the emulator
worker in the same pod; that worker sources ``scripts/android/stages/pipeline.sh``
and writes native metrics for normalization here. It never submits another deployment platform
job from inside a running pod.

Pure ``metrics_to_state`` and the worker boundary are unit-tested; the real
emulator invocation remains canary-gated.
"""

from __future__ import annotations

import json
from pathlib import Path

from core import exit_contract
from core.inpod import failed_state, run_worker, worker_env
from core.normalize import normalize
from core.pipeline import PipelineConfig, stages_for

PLATFORM = "android"
PASS_RULE = "exit_and_stages"
WORKER = Path(__file__).with_name("worker.sh")


def _stage_outcomes(stages: dict, metrics: dict, native_exit: int | None) -> dict:
    eval_status = str(metrics.get("eval_status") or "")
    forced_outcome = None
    forced_reason = ""
    if eval_status in ("agent_api_exhausted", "pipeline_error", "report_missing"):
        forced_outcome = exit_contract.OUTCOME_INFRA_ERROR
        forced_reason = {
            "agent_api_exhausted": "model_api_exhausted",
            "pipeline_error": "android_pipeline_error",
            "report_missing": "eval_result_missing",
        }[eval_status]
    elif eval_status.startswith("skipped_"):
        forced_outcome = exit_contract.OUTCOME_DATA_ERROR
        forced_reason = "invalid_frozen_tests"

    if forced_outcome is not None:
        names = tuple(stages)
        target = next(
            (name for name in names if stages[name] != exit_contract.STAGE_PASS),
            "eval" if "eval" in stages else names[0],
        )
        target_index = names.index(target)
        return {
            name: exit_contract.stage_outcome(
                status=(
                    exit_contract.STAGE_PASS
                    if index < target_index
                    else (
                        (
                            exit_contract.STAGE_FAIL
                            if forced_outcome == exit_contract.OUTCOME_DATA_ERROR
                            else exit_contract.STAGE_ERROR
                        )
                        if index == target_index
                        else exit_contract.STAGE_NOT_RUN
                    )
                ),
                outcome_class=(
                    exit_contract.OUTCOME_COMPLETED
                    if index < target_index
                    else forced_outcome
                ),
                reason_code=(
                    "completed"
                    if index < target_index
                    else (
                        forced_reason
                        if index == target_index
                        else "upstream_stage_failed"
                    )
                ),
                native_exit_code=native_exit if index == target_index else None,
            )
            for index, name in enumerate(names)
        }

    authoritative_result = bool(
        native_exit in (None, exit_contract.RC_OK)
        and (
            eval_status == "no_usable_artifact"
            or metrics.get("task_score") is not None
            or metrics.get("excluded_from_scoring")
            or (
                stages
                and all(
                    status == exit_contract.STAGE_PASS for status in stages.values()
                )
            )
        )
    )
    if authoritative_result:
        return {
            name: exit_contract.stage_outcome(
                status=status,
                outcome_class=exit_contract.OUTCOME_COMPLETED,
                reason_code=(
                    "completed"
                    if status == "pass"
                    else (
                        "no_usable_artifact"
                        if name == "recreation"
                        else "not_evaluated_no_artifact"
                    )
                ),
                native_exit_code=native_exit if name == next(iter(stages)) else None,
            )
            for name, status in stages.items()
        }

    outcome = exit_contract.from_native_exit(
        native_exit if native_exit is not None else exit_contract.RC_INFRA
    )
    if "setup" in stages and outcome == exit_contract.OUTCOME_DATA_ERROR:
        outcome = exit_contract.OUTCOME_INFRA_ERROR
    reason = {
        exit_contract.OUTCOME_DATA_ERROR: "invalid_frozen_input",
        exit_contract.OUTCOME_INFRA_ERROR: "android_worker_failed",
        exit_contract.OUTCOME_TERMINATED: "worker_terminated",
    }[outcome]
    failed_name = next(
        (name for name, status in stages.items() if status != exit_contract.STAGE_PASS),
        next(iter(stages)),
    )
    return {
        name: exit_contract.stage_outcome(
            status=(
                exit_contract.STAGE_PASS
                if status == exit_contract.STAGE_PASS
                else (
                    exit_contract.STAGE_NOT_RUN
                    if status == exit_contract.STAGE_NOT_RUN
                    else (
                        exit_contract.STAGE_TIMEOUT
                        if outcome == exit_contract.OUTCOME_TERMINATED
                        else (
                            exit_contract.STAGE_FAIL
                            if outcome == exit_contract.OUTCOME_DATA_ERROR
                            else exit_contract.STAGE_ERROR
                        )
                    )
                )
            ),
            outcome_class=(
                exit_contract.OUTCOME_COMPLETED if status == "pass" else outcome
            ),
            reason_code=(
                "completed"
                if status == exit_contract.STAGE_PASS
                else (
                    "upstream_stage_failed"
                    if status == exit_contract.STAGE_NOT_RUN
                    else reason
                )
            ),
            native_exit_code=native_exit if name == failed_name else None,
        )
        for name, status in stages.items()
    }


def metrics_to_state(task_id: str, model: str, m: dict) -> dict:
    """Map android's per-app metrics.json into the unified pipeline state.

    android already computes task_score (passed/manifest_total) + program/vlm; we
    pass them straight through as overrides.
    """
    m = m or {}
    # The native writer already records the stages that actually ran.  This matters
    # for setup-only canaries: deriving release stages from a missing APK/score turns
    # a successful {"setup":"ok"} into two fabricated recreation/eval failures.
    stages = m.get("stages") if isinstance(m.get("stages"), dict) else None
    if not stages:
        recreation_ok = bool(m.get("recreation_success", False))
        eval_ok = (
            m.get("eval_status") in ("ok", "success", None)
            and m.get("task_score") is not None
        )
        stages = {
            "recreation": "pass" if recreation_ok else "fail",
            "eval": "pass" if (recreation_ok and eval_ok) else "fail",
        }
    native_exit = m.get("pipeline_exit_code", m.get("exit_code"))
    if not isinstance(native_exit, int):
        native_exit = None
    st = normalize(
        task_id,
        model,
        stages,
        {},
        summary={"eval_status": m.get("eval_status")},
        pipeline_exit=native_exit,
    )
    st["task_score"] = m.get("task_score")
    st["program_score"] = m.get("program_score")
    st["vlm_score"] = m.get("vlm_score")
    if m.get("score_passed") is not None:
        st["score_passed"] = bool(m["score_passed"])
    if m.get("passed") is not None:
        st["passed"] = bool(m["passed"])
    if m.get("excluded_from_scoring"):
        st["excluded_from_scoring"] = True
        st["exclusion_reason"] = m.get("exclusion_reason")
    if native_exit is not None:
        st["native_exit_code"] = native_exit
    exit_contract.apply_stage_outcomes(
        st,
        _stage_outcomes(st["stages"], m, native_exit),
        tuple(st["stages"]),
    )
    return st


def run(task: dict, cfg: PipelineConfig) -> dict:
    """Run the Android lifecycle against a prepared ADB target."""
    if cfg.target.kind != "adb" or not cfg.target.device:
        raise RuntimeError(
            "Android requires a prepared ADB target; start it first and pass --device"
        )
    runner = WORKER
    path = Path(cfg.output_dir) / "metrics.json"
    path.unlink(missing_ok=True)
    rc = run_worker(runner, cfg, env=worker_env(task, cfg))
    try:
        m = json.loads(path.read_text())
    except Exception as exc:
        if rc == exit_contract.RC_DATA:
            outcome = exit_contract.OUTCOME_DATA_ERROR
            reason = "invalid_frozen_input"
        elif rc in (130, exit_contract.RC_TIMEOUT):
            outcome = exit_contract.OUTCOME_TERMINATED
            reason = "android_worker_terminated"
        else:
            outcome = exit_contract.OUTCOME_INFRA_ERROR
            reason = "android_worker_result_missing"
        return failed_state(
            task["task_id"],
            cfg.model,
            list(stages_for(cfg.stage)),
            rc,
            f"Android worker produced no readable metrics: {exc}",
            outcome_class=outcome,
            reason_code=reason,
        )
    selected = stages_for(cfg.stage)
    native_stages = m.get("stages")
    if not isinstance(native_stages, dict) or any(
        name not in native_stages for name in selected
    ):
        return failed_state(
            task["task_id"],
            cfg.model,
            list(selected),
            rc or 2,
            "Android native metrics has incomplete per-stage evidence",
        )
    if (
        "eval" in selected
        and m.get("task_score") is None
        and not m.get("excluded_from_scoring")
    ):
        return failed_state(
            task["task_id"],
            cfg.model,
            list(selected),
            rc or 2,
            "Android eval produced no task score",
        )
    state = metrics_to_state(task["task_id"], cfg.model, m)
    if rc:
        state["pipeline_exit"] = rc
        state["native_exit_code"] = rc
        state["passed"] = False
        exit_contract.override_pipeline_outcome(
            state,
            selected,
            outcome_class=exit_contract.from_native_exit(rc),
            reason_code=(
                "android_worker_terminated"
                if exit_contract.from_native_exit(rc)
                == exit_contract.OUTCOME_TERMINATED
                else "android_worker_failed"
            ),
            native_exit_code=rc,
        )
    return state
