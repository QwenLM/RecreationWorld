#!/usr/bin/env python3
"""The metrics.json an deployment platform *template* writes post-mortem, for all platforms.

Distinct from ``core.metrics_contract.build_metrics``, which the Python sandbox controllers call
with a live ``stages``/``sandbox_info``/``summary`` state. THIS module covers the other
position: main.sh runs it after the controller has already exited -- including when the
controller crashed, was SIGTERMed at the wall clock, or never started. Its only inputs are
the eval report files on disk plus the pipeline's exit code, because that is all that is
still knowable at that point.

Why it lives in rb rather than in each template: it was 87 lines of scoring logic inlined in
``task/recreation-bench-linux/main.sh``, whose own comments said three times that it was a
hand-copy of ``core.metrics_contract`` ("Same vocabulary core/metrics_contract.py uses for the other
four platforms"). main.sh is the deployment platform adapter and must not carry benchmark logic. Moved
verbatim, so the numbers are unchanged.

Why the template still needs a fallback when this import fails: this IS the last-resort
reporting path. If the rb tarball could not be fetched, the template must still leave deployment platform a
metrics.json; if core cannot be imported, the template keeps any result already on disk.

Usage from a template (linux):
    OUTPUT_DIR=... TASK_ID=... STAGE=... WM_EXIT=... WM_FORCE=... \
      PYTHONPATH="$RB_PIPELINE_DIR/scripts" python3 -m core.template_metrics
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

from core import exit_contract

# Only these stage selections measure the recreation; every other selection is ungraded. Same
# distinction core.metrics_contract draws between release execution and setup diagnostics.
EVAL_STAGES = ("eval", "recreation_eval")


def _requested_stages(stage: str) -> tuple[str, ...]:
    if stage in ("recreation_eval", "all"):
        return ("recreation", "eval")
    if stage in ("setup", "recreation", "eval"):
        return (stage,)
    # A last-resort writer must still produce a contract-shaped result. Unknown
    # lifecycle input is a controller/configuration failure, represented by one
    # synthetic stage rather than an empty (un-aggregatable) outcome map.
    return ("pipeline",)


def _contract_fields(
    *,
    stage: str,
    native_exit_code: int,
    force: str,
    eval_status: str,
    has_eval_report: bool,
) -> dict:
    """Build the same public outcome envelope as the live shared controller."""
    if force == "pod_timeout" or native_exit_code == exit_contract.RC_TIMEOUT:
        outcome = exit_contract.OUTCOME_TERMINATED
        reason = "pod_terminated"
    elif force:
        outcome = exit_contract.OUTCOME_INFRA_ERROR
        reason = force
    elif native_exit_code == exit_contract.RC_OK and eval_status in (
        "report_missing",
        "no_tests",
    ):
        # A zero child status without the requested score evidence is a protocol
        # failure, not a completed model-zero result.
        outcome = exit_contract.OUTCOME_INFRA_ERROR
        reason = eval_status
    else:
        outcome = exit_contract.from_native_exit(native_exit_code)
        reason = {
            exit_contract.OUTCOME_COMPLETED: "completed",
            exit_contract.OUTCOME_DATA_ERROR: "invalid_frozen_input",
            exit_contract.OUTCOME_INFRA_ERROR: "pipeline_error",
            exit_contract.OUTCOME_TERMINATED: "pod_terminated",
        }[outcome]

    names = _requested_stages(stage)
    if outcome == exit_contract.OUTCOME_COMPLETED:
        outcomes = {
            name: exit_contract.stage_outcome(
                status=exit_contract.STAGE_PASS,
                outcome_class=outcome,
                reason_code=reason,
                native_exit_code=native_exit_code if index == len(names) - 1 else None,
            )
            for index, name in enumerate(names)
        }
    else:
        # A readable eval report proves recreation was reached. Otherwise the
        # post-mortem writer cannot safely claim that any earlier stage passed.
        target_index = names.index("eval") if has_eval_report and "eval" in names else 0
        outcomes = {}
        for index, name in enumerate(names):
            if index < target_index:
                outcomes[name] = exit_contract.stage_outcome(
                    status=exit_contract.STAGE_PASS,
                    outcome_class=exit_contract.OUTCOME_COMPLETED,
                    reason_code="completed",
                )
            elif index == target_index:
                status = (
                    exit_contract.STAGE_TIMEOUT
                    if outcome == exit_contract.OUTCOME_TERMINATED
                    else (
                        exit_contract.STAGE_FAIL
                        if outcome == exit_contract.OUTCOME_DATA_ERROR
                        else exit_contract.STAGE_ERROR
                    )
                )
                outcomes[name] = exit_contract.stage_outcome(
                    status=status,
                    outcome_class=outcome,
                    reason_code=reason,
                    native_exit_code=native_exit_code,
                )
            else:
                outcomes[name] = exit_contract.stage_outcome(
                    status=exit_contract.STAGE_NOT_RUN,
                    outcome_class=outcome,
                    reason_code="upstream_stage_failed",
                )

    aggregate = exit_contract.aggregate_stage_outcomes(outcomes, names)
    return {
        "pipeline_exit_code": aggregate["process_exit_code"],
        "stage_outcomes": outcomes,
        **aggregate,
        **{
            f"stage_{name}": (
                "succeeded" if item["status"] == exit_contract.STAGE_PASS else "failed"
            )
            for name, item in outcomes.items()
        },
    }


def _load(p) -> Optional[dict]:
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return None


def _sub(d: Any) -> Optional[dict]:
    if not isinstance(d, dict):
        return None
    try:
        pr = float(d.get("pass_rate", 0.0) or 0.0)
    except (TypeError, ValueError):
        pr = 0.0
    return {
        "passed": int(d.get("passed", 0) or 0),
        "total": int(d.get("total", 0) or 0),
        "pass_rate": round(pr, 4),
        "skipped": d.get("skipped", ""),
    }


def build(
    *,
    output_dir: Path,
    task_id: str,
    stage: str,
    exit_code: int,
    force: str = "",
    eval_dir: Optional[Path] = None,
) -> dict:
    """The metrics dict, without touching the filesystem beyond reading the eval reports.

    ``eval_dir`` defaults to the linux layout; a platform whose reports live elsewhere passes
    its own so the classification below stays one copy.
    """
    if eval_dir is None:
        eval_dir = output_dir / "pipeline_state" / task_id / "eval"
    prog = _sub(_load(eval_dir / "programmatic_results.json"))
    vlm = _sub(_load(eval_dir / "vlm_results.json"))

    excluded_reason = None
    if force:
        status, passed, score = force, False, 0.0
    elif stage not in EVAL_STAGES:
        passed = exit_code == 0
        status = "not_evaluated" if passed else "pipeline_error"
        # NOT 1.0. This range contains no eval, so nothing was graded, and a field named
        # task_score must not carry a number nobody measured -- announcing a PERFECT one is
        # the worst available shape, because any sweep that aggregates by variant averages it
        # in as a graded pass. Completion is already reported by `passed` (and spelled out by
        # eval_status=not_evaluated). Same vocabulary core/metrics_contract.py uses.
        score = None
        excluded_reason = (
            f"stage={stage!r} contains no recreation eval (EVAL_STAGES={list(EVAL_STAGES)}), "
            "so no recreation was graded; agent completion is in `passed`"
        )
    elif prog is None:
        status = "pipeline_error" if exit_code != 0 else "report_missing"
        passed, score = False, 0.0
    elif prog["total"] <= 0:
        status, passed, score = "no_tests", False, 0.0
    else:
        status = "resolved" if exit_code == 0 else "failed"
        passed = status == "resolved"
        score = prog["pass_rate"]

    # Flat top-level mirrors of the nested programmatic/vlm pass-rates, so the deployment platform's
    # A single-field external aggregation can average program_score / vlm_score /
    # prog_vlm_avg into the unified group macros (it only avg/sums ONE top-level field).
    # An UNGRADED dimension is None, never 0 -- and "graded" means total > 0, not merely that
    # the file exists: a judge with no assertions writes {"total": 0, "pass_rate": 0}.
    # Averaging an absent VLM as zero HALVES a real programmatic score. Not hypothetical:
    # core.metrics_contract records program 0.5291 surfacing as prog_vlm_avg 0.26455 on web, and a
    # A macOS canary (2026-08-20) produced exactly this condition: prog 21/89 with
    # VLM total 0.
    _ps = float(prog["pass_rate"]) if (prog and (prog.get("total") or 0) > 0) else None
    _vs = float(vlm["pass_rate"]) if (vlm and (vlm.get("total") or 0) > 0) else None
    _measured = [v for v in (_ps, _vs) if v is not None]
    metrics = {
        "task_id": task_id,
        "stage": stage,
        "task_score": score,
        "passed": passed,
        "eval_status": status,
        "program_score": _ps,
        "vlm_score": _vs,
        "prog_vlm_avg": (
            round(sum(_measured) / len(_measured), 6) if _measured else None
        ),
        "programmatic": prog,
        "vlm": vlm,
    }
    metrics.update(
        _contract_fields(
            stage=stage,
            native_exit_code=exit_code,
            force=force,
            eval_status=status,
            has_eval_report=prog is not None,
        )
    )
    if exit_code != metrics["process_exit_code"]:
        metrics["native_exit_code"] = exit_code
    if score is None:
        # Marked rather than left implicit, so aggregation skips this task instead of reading
        # the absence as a zero (or, before this, as a perfect score).
        metrics["excluded_from_scoring"] = True
        metrics["score_absent_reason"] = (
            excluded_reason or "no recreation eval in this run"
        )
    return metrics


def main(argv: Optional[list[str]] = None) -> int:
    """Env-driven entry point -- the template passes the same variables it always did."""
    output_dir = Path(os.environ["OUTPUT_DIR"])
    metrics = build(
        output_dir=output_dir,
        task_id=os.environ["TASK_ID"],
        stage=os.environ.get("STAGE", "all"),
        exit_code=int(os.environ.get("WM_EXIT", "0") or 0),
        force=os.environ.get("WM_FORCE", ""),
    )
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "metrics.json").write_text(
        json.dumps(metrics, indent=2, ensure_ascii=False)
    )
    print(
        f"[metrics] eval_status={metrics['eval_status']} "
        f"passed={metrics['passed']} task_score={metrics['task_score']}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
