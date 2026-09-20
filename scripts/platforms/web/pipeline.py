#!/usr/bin/env python3
"""RecreationBench Web pipeline for the platform worker runtime.

deployment adapter enters ``core/pipeline.py`` first. This module launches the
security-sensitive native rollout/evaluation worker in the same pod, then maps
its result into the unified state, supplying the
weighted ``final_score`` as the task_score OVERRIDE (web's 4-dim weighting is
mandatorily different and preserved, not reimplemented).

Pure ``result_to_state`` (aggregator result -> unified state) and the worker
boundary are unit-tested; the real browser invocation remains canary-gated.
"""

from __future__ import annotations

import json
from pathlib import Path

from core import exit_contract
from core.inpod import failed_state, run_worker, worker_env
from core.normalize import normalize
from core.pipeline import PipelineConfig, stages_for

PLATFORM = "web"
PASS_RULE = "exit_and_stages"
WORKER = Path(__file__).with_name("worker.sh")


def _programmatic_only(scores: dict, fallback) -> object:
    """web's PROGRAMMATIC score, with the VLM-graded dimension taken back out.

    prog_vlm_avg is meant to be a clean 50/50 of program_score and vlm_score, the way it
    already is on the four pytest platforms. web broke that: score_aggregator sets
    ``breakdown["visual"] = vlm_score`` whenever the judge runs and then folds it into
    ``test_score``, so using test_score as program_score counted the judge twice —

        program_score = 0.50*functional + 0.50*vlm
        prog_vlm_avg  = 0.25*functional + 0.75*vlm

    i.e. 75% of web's headline number was the VLM judge, against windows' 50/50, and the
    two were being compared as if they measured the same thing.

    So drop the visual dimension and renormalize over the weights that remain. With the
    shipped config.TEST_WEIGHTS (functional .50 / visual .50 / structural 0 / quality 0)
    that is exactly the functional pass-rate — structural and quality carry weight 0, so
    nothing measurable is discarded — and prog_vlm_avg becomes 0.5*functional + 0.5*vlm.
    Reading the weights rather than hardcoding it keeps that true if structural/quality
    are ever given weight again.

    ``task_score`` is deliberately untouched: it stays web's own authoritative
    ``final_score``, which is what web reports natively.
    """
    weights = scores.get("weights") if isinstance(scores.get("weights"), dict) else {}
    breakdown = (
        scores.get("breakdown") if isinstance(scores.get("breakdown"), dict) else {}
    )
    prog = {k: float(v) for k, v in weights.items() if k != "visual" and v}
    if prog and breakdown:
        denom = sum(prog.values())
        if denom > 0:
            return round(
                sum(w * float(breakdown.get(k) or 0.0) for k, w in prog.items())
                / denom,
                6,
            )
    # No weights emitted (an older scorer): the functional pass-rate is the honest
    # stand-in. test_score is the LAST resort because it re-introduces the double count.
    if isinstance(breakdown.get("functional"), (int, float)):
        return round(float(breakdown["functional"]), 6)
    return scores.get("test_score", fallback)


def result_to_state(
    task_id: str,
    model: str,
    native: dict,
    requested_stages: tuple[str, ...] = ("recreation", "eval"),
) -> dict:
    """Map web's native result into the unified pipeline state.

    Web's AUTHORITATIVE scoring — the 4-dimension weighted ``final_score`` and its 0.75
    score threshold — lives in the Web evaluator's Phase-4 writer, not here. This
    preserves web's task_score / score_passed verdict and adds the unified
    contract's standard fields; it never re-derives the score. Accepts either web's own
    ``metrics.json`` (top-level task_score/passed/excluded + nested ``metrics``) or the
    raw score-aggregator dict ({final_score, test_score, vlm_score})."""
    native = native or {}
    inner = native.get("metrics") if isinstance(native.get("metrics"), dict) else {}
    scores = inner.get("scores") if isinstance(inner.get("scores"), dict) else {}
    task_score = native.get("task_score", native.get("final_score"))
    final = inner.get("final_score", native.get("final_score", task_score))
    status = inner.get("status", native.get("status"))
    no_submission_reason = inner.get(
        "no_submission_reason", native.get("no_submission_reason")
    )
    api_failure = status == "api_error" or (
        status == "no_submission" and no_submission_reason == "api_error"
    )
    agent_infra = api_failure or status in ("agent_not_available", "agent_error")
    agent_terminated = status == "agent_terminated"
    rollout = inner.get("rollout", native.get("rollout"))
    rollout = rollout if isinstance(rollout, dict) else {}
    agent_native_rc = rollout.get("native_returncode")
    if isinstance(agent_native_rc, bool) or not isinstance(agent_native_rc, int):
        agent_native_rc = None
    api_error = inner.get("api_error", native.get("api_error")) or rollout.get(
        "api_error"
    )
    ran = task_score is not None or final is not None or status is not None
    # A low score is a valid result, but `no_submission` is not: the worker can exit zero after
    # scoring the untouched template when the agent API exhausts its retries.  Keep eval marked
    # as executed while failing recreation, so deployment platform reports the actual failed boundary.
    #
    # `api_error` is the same boundary reached from the other side: the rollout died on an
    # upstream failure AFTER authoring part of the site, so submitted stays True and only the
    # status distinguishes it from a genuine low score.
    submitted = inner.get("submitted", native.get("submitted"))
    recreated = (
        ran
        and submitted is not False
        and not agent_infra
        and not agent_terminated
        and status != "no_submission"
    )
    all_stages = {
        "recreation": (
            "pass" if recreated else "timeout" if agent_terminated else "fail"
        ),
        "eval": (
            "not_run" if agent_infra or agent_terminated else "pass" if ran else "fail"
        ),
    }
    stages = {name: all_stages[name] for name in requested_stages}
    st = normalize(
        task_id,
        model,
        stages,
        {},
        summary={
            "scores": scores or native.get("breakdown"),
            "status": status,
            "submitted": submitted,
            "no_submission_reason": no_submission_reason,
            "api_error": api_error,
            "failure_hint": str(api_error) if api_failure and api_error else "",
        },
    )
    st["task_score"] = task_score
    st["program_score"] = native.get(
        "program_score", _programmatic_only(scores, task_score)
    )
    st["vlm_score"] = native.get(
        "vlm_score", scores.get("vlm_score", scores.get("visual"))
    )
    if native.get("passed") is not None:
        # `passed` in the five-platform contract means the requested pipeline stages ran
        # successfully.  Web's native `passed` is instead a score-threshold verdict; carrying
        # it as the top-level value made a healthy, fully graded low-score run look like an deployment platform
        # infrastructure failure.  Keep that information under an explicit name.
        st["score_passed"] = bool(native["passed"])
    if native.get("excluded_from_scoring"):  # explicit unscored result
        st["excluded_from_scoring"] = True
        st["exclusion_reason"] = native.get("exclusion_reason")
    if agent_infra:
        outcomes = {
            name: exit_contract.stage_outcome(
                status="error" if name == "recreation" else "not_run",
                outcome_class=exit_contract.OUTCOME_INFRA_ERROR,
                reason_code=(
                    "model_api_exhausted"
                    if name == "recreation" and api_failure
                    else (
                        "agent_process_failed"
                        if name == "recreation"
                        else "upstream_stage_failed"
                    )
                ),
                native_exit_code=(agent_native_rc if name == "recreation" else None),
            )
            for name in stages
        }
    elif agent_terminated:
        outcomes = {
            name: exit_contract.stage_outcome(
                status="timeout" if name == "recreation" else "not_run",
                outcome_class=exit_contract.OUTCOME_TERMINATED,
                reason_code=(
                    "external_termination"
                    if name == "recreation"
                    else "upstream_stage_terminated"
                ),
                native_exit_code=(
                    exit_contract.RC_TIMEOUT if name == "recreation" else None
                ),
            )
            for name in stages
        }
    else:
        # Low score, refusal, and a genuine no-code submission are all authoritative
        # model outcomes. They are not retried as infrastructure.
        outcomes = {
            name: exit_contract.stage_outcome(
                status=stage_status,
                outcome_class=exit_contract.OUTCOME_COMPLETED,
                reason_code=(
                    "completed" if stage_status == "pass" else "no_usable_submission"
                ),
            )
            for name, stage_status in stages.items()
        }
    exit_contract.apply_stage_outcomes(st, outcomes, tuple(stages))
    return st


def run(task: dict, cfg: PipelineConfig) -> dict:
    """Run the in-pod Web lifecycle and return one normalized state."""
    runner = WORKER

    if cfg.stage == "setup":
        path = Path(cfg.output_dir) / "permission_report.json"
        path.unlink(missing_ok=True)
        rc = run_worker(
            runner,
            cfg,
            env={**worker_env(task, cfg), "RB_PERM_SETUP": "1"},
        )
        try:
            report = json.loads(path.read_text())
        except Exception as exc:
            return failed_state(
                task["task_id"],
                cfg.model,
                ["setup"],
                rc,
                f"Web worker produced no readable permission report: {exc}",
            )
        passed = report.get("passed") is True and rc == 0
        state = normalize(
            task["task_id"],
            cfg.model,
            {"setup": "pass" if passed else "fail"},
            {},
            summary={"permission_report": report},
            pipeline_exit=rc or (0 if passed else 2),
        )
        state["passed"] = passed
        state["native_exit_code"] = rc
        outcome = (
            exit_contract.OUTCOME_COMPLETED
            if passed
            else exit_contract.OUTCOME_INFRA_ERROR
        )
        exit_contract.apply_stage_outcomes(
            state,
            {
                "setup": exit_contract.stage_outcome(
                    status="pass" if passed else "error",
                    outcome_class=outcome,
                    reason_code="completed" if passed else "permission_setup_failed",
                    native_exit_code=rc,
                )
            },
            ("setup",),
        )
        return state

    if cfg.stage not in ("eval", "recreation_eval"):
        raise RuntimeError(
            "Web release execution supports recreation_eval or standalone eval"
        )
    if cfg.stage == "eval" and cfg.eval_target != "reference":
        raise RuntimeError(
            "Web standalone eval currently requires eval_target=reference; "
            "no recreation artifact was supplied to this pod"
        )

    path = Path(cfg.output_dir) / "metrics.json"
    path.unlink(missing_ok=True)
    rc = run_worker(runner, cfg, env=worker_env(task, cfg))
    try:
        native = json.loads(path.read_text())
    except Exception as exc:
        outcome = exit_contract.from_native_exit(
            rc or exit_contract.RC_INFRA,
            one=exit_contract.OUTCOME_DATA_ERROR,
        )
        return failed_state(
            task["task_id"],
            cfg.model,
            list(stages_for(cfg.stage)),
            rc,
            f"Web worker produced no readable metrics: {exc}",
            outcome_class=outcome,
            reason_code=(
                "web_worker_terminated"
                if outcome == exit_contract.OUTCOME_TERMINATED
                else (
                    "invalid_frozen_input"
                    if outcome == exit_contract.OUTCOME_DATA_ERROR
                    else "web_worker_result_missing"
                )
            ),
        )
    if native.get("passed") is None:
        return failed_state(
            task["task_id"],
            cfg.model,
            list(stages_for(cfg.stage)),
            rc or 2,
            "Web native metrics has no score-threshold verdict",
            outcome_class=exit_contract.from_native_exit(
                rc or 2, one=exit_contract.OUTCOME_DATA_ERROR
            ),
            reason_code="web_native_result_invalid",
        )

    state = result_to_state(
        task["task_id"],
        cfg.model,
        native,
        requested_stages=stages_for(cfg.stage),
    )
    state["native_exit_code"] = rc
    # Preserve a specific runner diagnosis (e.g. model_api_exhausted) when the
    # worker's nonzero exit confirms it. A contradictory wrapper exit still wins.
    if rc and exit_contract.from_native_exit(rc) != state["outcome_class"]:
        exit_contract.override_pipeline_outcome(
            state,
            stages_for(cfg.stage),
            outcome_class=exit_contract.from_native_exit(
                rc, one=exit_contract.OUTCOME_DATA_ERROR
            ),
            reason_code=(
                "web_worker_terminated"
                if exit_contract.from_native_exit(rc)
                == exit_contract.OUTCOME_TERMINATED
                else "invalid_frozen_input" if rc == 1 else "web_worker_failed"
            ),
            native_exit_code=rc,
        )
    if rc:
        state["passed"] = False
    return state
