#!/usr/bin/env python3
"""Android per-app ``metrics.json`` computation.

The platform adapter supplies runtime inputs and collects artifacts; benchmark scoring remains
in this provider-neutral module. Its output preserves the native Android score while exposing the
shared five-platform fields.

ENV CONTRACT (all read via os.environ, defaults preserved from the heredoc):
    OUTPUT_DIR       where metrics.json is written (default /workspace/output)
    EVAL_DIR         eval outputs to read the grade from
    STAGE            raw controller stage value, kept for the record
    STAGE_LIST       NORMALIZED stage list -- the eval decision reads this, never STAGE
    WM_EXIT          pipeline exit code
    WM_FORCE         forced status ("dataset_error" / "pipeline_error") from die()
    WM_STAGES        per-stage status JSON from stage_status_json
    TASK_ID SOURCE_TASK_ID INSTANCE_ID PASS_ID AAR_VERSION RB_ARTIFACT_RUN_PREFIX
    MODEL RECREATION_MODEL

Writes ``{OUTPUT_DIR}/metrics.json`` and prints it for the controller log.
"""

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core import agent_invocation, trajectory  # noqa: E402

output_dir = os.environ.get("OUTPUT_DIR", "/workspace/output")
eval_dir = os.environ.get("EVAL_DIR", "")
stage = os.environ.get("STAGE", "")
wm_exit = int(os.environ.get("WM_EXIT", "0") or 0)
wm_force = os.environ.get("WM_FORCE", "")
# Decide from the NORMALIZED stage list, never from the raw STAGE string. STAGE can
# be a legacy alias or "recreation,eval"; only the normalized list is authoritative.
stage_list = [s for s in os.environ.get("STAGE_LIST", "").split(",") if s]
ran_eval = "eval" in stage_list
try:
    native_stages = json.loads(os.environ.get("WM_STAGES") or "{}")
except (TypeError, ValueError, json.JSONDecodeError):
    native_stages = {}
recreation_artifact_present = os.path.isfile(
    os.path.join(output_dir, "recreation", "recreated.apk")
)
# A file called recreated.apk is not itself proof of a usable recreation.  The
# worker validates the archive/package before recording the stage as ``ok``.
recreation_success = bool(
    recreation_artifact_present
    and native_stages.get("recreation") in ("ok", "reused")
)

# The shell deliberately stops a full recreation+eval run with exit 0 when the
# agent finished but produced no usable APK (including a non-empty but invalid
# APK).  Its stage evidence is authoritative:
# recreation=failed and eval=skipped means there was nothing to evaluate, not that
# an eval report went missing.  Keep the predicate narrow so a genuinely absent
# report after recreation/eval both ran still fails closed as report_missing.
no_usable_artifact = bool(
    wm_exit == 0
    and native_stages.get("recreation") == "failed"
    and native_stages.get("eval") in (None, "skipped")
)


def _terminal_api_failure(output_dir):
    """Whether the shared terminal record identifies an upstream/API failure."""
    return (
        agent_invocation.terminal_api_failure(
            os.path.join(output_dir, "trajectory.jsonl")
        )
        is not None
    )


def _sub_metric(d):
    """One deployment platform sub-metric in recreation-bench-linux's shape.

    That template emits `programmatic` and `vlm` as {passed,total,pass_rate,skipped},
    and the platform's dashboards read that shape. Emitting the same thing here means
    Android and Linux app-recreation runs are directly comparable in deployment platform instead of
    each needing its own reader.
    """
    if not isinstance(d, dict):
        return None
    try:
        rate = float(d.get("pass_rate", 0.0) or 0.0)
    except (TypeError, ValueError):
        rate = 0.0
    return {
        "passed": int(d.get("passed", 0) or 0),
        "total": int(d.get("total", 0) or 0),
        "pass_rate": round(rate, 4),
        "skipped": d.get("skipped", ""),
    }


def _rate(d):
    """A sub-metric's pass rate as a bare float, or None when it never ran.

    None rather than 0.0 for the same reason task_score is null on a non-eval job: a
    zero that means "not measured" is indistinguishable from a real zero once averaged.
    """
    if not isinstance(d, dict):
        return None
    try:
        return round(float(d.get("pass_rate", 0.0) or 0.0), 4)
    except (TypeError, ValueError):
        return None


def _count(d):
    """A sub-metric's case count, i.e. how many manifest cases of that kind exist."""
    if not isinstance(d, dict):
        return None
    try:
        return int(d.get("total", 0) or 0)
    except (TypeError, ValueError):
        return None


def _trajectory_stage(output_dir):
    try:
        with open(os.path.join(output_dir, ".trajectory_stage")) as f:
            return f.read().strip()
    except Exception:
        return ""


metrics = {
    "task_id": os.environ.get("TASK_ID", ""),
    "instance_id": os.environ.get("INSTANCE_ID", os.environ.get("TASK_ID", "")),
    "source_task_id": os.environ.get("SOURCE_TASK_ID", ""),
    "pass_id": os.environ.get("PASS_ID", ""),
    "version": os.environ.get("AAR_VERSION", ""),
    "artifact_prefix": os.environ.get("RB_ARTIFACT_RUN_PREFIX", ""),
    "stage": stage,
    "stage_list": ",".join(stage_list),
    # Per-stage outcome: ok | reused | failed | skipped | below_gate | no_testcases.
    # The exit code says whether retrying is worth it; this says what happened.
    "stages": native_stages,
    "model": os.environ.get("MODEL", ""),
    "recreation_model": os.environ.get("RECREATION_MODEL", os.environ.get("MODEL", "")),
    "exit_code": wm_exit,
    "pipeline_exit_code": wm_exit,
    "task_score": 0.0,
    "passed": False,
    "eval_status": "unknown",
    # Averaged per group by extra_aggregations. These follow task_score's decision
    # EXACTLY: 0.0 for a trial that entered eval but graded no result (so it stays in
    # the group denominator instead of dropping out), and null ONLY for a job that
    # never entered eval at all (for example recreation-only), where task_score is
    # null too. The no-result branches below set these to 0.0 alongside task_score.
    "program_score": None,
    "vlm_score": None,
    "testcases": None,
    "testcases_prog": None,
    "testcases_vlm": None,
    "recreation_success": recreation_success,
}

# 复用 eval 阶段 test_verify.sh 产出的 metrics.json
ev = None
try:
    ev = json.load(open(f"{eval_dir}/metrics.json"))
    metrics["task_score"] = float(ev.get("task_score", 0.0) or 0.0)
    # Preserve the native score-threshold verdict separately.  Top-level `passed` is the
    # unified pipeline-health result and is set below from the worker exit/stage outcome.
    if ev.get("passed") is not None:
        metrics["score_passed"] = bool(ev["passed"])
    metrics["passed"] = bool(ev.get("passed", False))
    metrics["eval_modules"] = ev.get("modules", {})
    metrics["eval_total_cases"] = ev.get("total_cases", 0)
    metrics["eval_passed_cases"] = ev.get("passed_cases", 0)
    metrics["total_cases"] = ev.get("total_cases", 0)
    # The manifest size is the one denominator.
    metrics["manifest_total"] = ev.get("manifest_total")
    metrics["package"] = ev.get("package", "")
    metrics["scored_against"] = ev.get("scored_against", "")
    # deployment platform-facing sub-metrics. `rule` / `vlm` come from the frozen-manifest scorer, so
    # their denominators are the manifest's counts rather than whatever the recreation
    # managed to run.
    metrics["programmatic"] = _sub_metric(ev.get("rule"))
    metrics["vlm"] = _sub_metric(ev.get("vlm"))
    # Flat mirrors of the two sub-metrics plus the manifest's case counts. The EvalResult
    # page can only average a top-level numeric field (extra_aggregations supports avg
    # and sum over a field NAME), so a nested pass_rate is invisible there.
    metrics["program_score"] = _rate(ev.get("rule"))
    metrics["vlm_score"] = _rate(ev.get("vlm"))
    metrics["testcases"] = ev.get("manifest_total") or ev.get("total_cases") or 0
    metrics["testcases_prog"] = _count(ev.get("rule"))
    metrics["testcases_vlm"] = _count(ev.get("vlm"))
except Exception:
    ev = None

# eval_status 分级（对齐 recreation-bench-linux write_metrics_final）：
#   pod_timeout    — pod 被 SIGTERM/墙钟中断，产物不完整
#   not_evaluated  — recreation-only 正常收尾，没有分数可言
#   pipeline_error — 流水线非 0 退出且拿不到 eval 结果
#   report_missing — 该跑 eval 但没有 metrics.json
#   no_tests       — 有 metrics.json 但 total_cases<=0（用例没装上/没跑起来）
#   resolved/failed— 真正评过分
if wm_force:
    metrics["eval_status"] = wm_force
    metrics["passed"] = False
    metrics["task_score"] = 0.0
    metrics["program_score"] = 0.0
    metrics["vlm_score"] = 0.0
elif no_usable_artifact:
    # Model-completed zero: the requested eval could not run because the model
    # delivered no valid candidate.  It belongs in the score denominator and is
    # terminal/non-retryable.  A recreation-only job still has no numeric score.
    metrics["eval_status"] = "no_usable_artifact"
    metrics["passed"] = False
    metrics["reason"] = "recreation completed without a usable APK"
    metrics["task_score"] = 0.0 if ran_eval else None
    metrics["program_score"] = 0.0 if ran_eval else None
    metrics["vlm_score"] = 0.0 if ran_eval else None
elif not ran_eval:
    # No eval in this job, so there is no score.
    # `passed` means "the agent finished cleanly" instead. Use the same terminal
    # protocol classifier as the five launchers so Codex and Claude cannot diverge.
    trajectory_path = os.path.join(output_dir, "trajectory.jsonl")
    protocol_status = trajectory.agent_terminal_status(trajectory_path)
    terminal_record = trajectory.last_protocol_record(trajectory_path)
    if protocol_status != "unknown":
        metrics["passed"] = protocol_status == "completed"
        metrics["agent_protocol_status"] = protocol_status
        if terminal_record and terminal_record.get("type") == "result":
            metrics["agent_result_subtype"] = terminal_record.get("subtype", "")
            metrics["num_turns"] = terminal_record.get("num_turns")
    else:
        metrics["passed"] = wm_exit == 0
    metrics["trajectory_stage"] = _trajectory_stage(output_dir)
    metrics["eval_status"] = "not_evaluated" if metrics["passed"] else "pipeline_error"
    # null, NOT 1.0. A sentinel score makes every scan that ranks by task_score read
    # a setup-only job as a flawless recreation.
    metrics["task_score"] = None
    # program_score / vlm_score stay null here (default), matching task_score: no eval
    # ran, so there is no trial to count in the program/vlm denominator.
elif ev is None and _terminal_api_failure(output_dir):
    # The agent call died on the upstream after the shared proxy retry budget, so nothing was
    # ever measured. Excluding is what the unified contract already does for web's
    # refusal case: metrics_contract.assemble_metrics nulls task_score AND prog_vlm_avg so the
    # app leaves the denominator instead of contributing a 0 it never earned.
    metrics["eval_status"] = "agent_api_exhausted"
    metrics["passed"] = False
    metrics["excluded_from_scoring"] = True
    metrics["exclusion_reason"] = (
        "model API retries exhausted with a terminal protocol error — the agent call never "
        "completed, so no recreation was attempted (infra, not a model failure)"
    )
    metrics["task_score"] = None
    metrics["program_score"] = None
    metrics["vlm_score"] = None
elif ev is None:
    metrics["eval_status"] = "pipeline_error" if wm_exit != 0 else "report_missing"
    metrics["passed"] = False
    metrics["task_score"] = 0.0
    metrics["program_score"] = 0.0
    metrics["vlm_score"] = 0.0
elif str(ev.get("eval_status", "")).startswith("skipped_"):
    # eval 被有意跳过（例如这个 app 还没出题）——保留具体原因，不要退化成
    # 泛化的 no_tests，否则日志里看不出是"没题"还是"用例没跑起来"。
    metrics["eval_status"] = ev["eval_status"]
    metrics["reason"] = ev.get("reason", "")
    metrics["passed"] = False
    # A DELIBERATE skip measured nothing, so it must not contribute a 0 it never earned —
    # the same rule the not_evaluated and agent_api_exhausted branches already follow.
    # skipped_no_testcases means the BENCHMARK has no suite for this app (a data gap on our
    # side); scoring it 0 blames the model for our missing testcases and silently drags
    # every average down. NOTE this is deliberately narrower than the no_tests branch
    # below: there an eval DID run and found 0 cases, which can mean the recreated app
    # never installed — a real failure that must keep its 0.
    metrics["excluded_from_scoring"] = True
    metrics["exclusion_reason"] = (
        "eval skipped ("
        + str(ev.get("eval_status", ""))
        + "): "
        + str(ev.get("reason", ""))
        + " — nothing was measured"
    )
    metrics["task_score"] = None
    metrics["program_score"] = None
    metrics["vlm_score"] = None
elif int(ev.get("total_cases", 0) or 0) <= 0:
    metrics["eval_status"] = "no_tests"
    metrics["passed"] = False
    metrics["task_score"] = 0.0
    metrics["program_score"] = 0.0
    metrics["vlm_score"] = 0.0
else:
    metrics["eval_status"] = "resolved" if wm_exit == 0 else "failed"
    metrics["passed"] = wm_exit == 0
    # deployment platform documents task_score as a float; make sure a graded run always presents one.
    # NOTE the semantics differ from recreation-bench-linux on purpose: there
    # task_score is the PROGRAMMATIC pass rate, here it is (rule+vlm)/manifest_total,
    # because every Android number ever reported used the combined figure. The
    # RB-comparable value is metrics["programmatic"]["pass_rate"].
    try:
        metrics["task_score"] = round(float(metrics.get("task_score") or 0.0), 4)
    except (TypeError, ValueError):
        metrics["task_score"] = 0.0

os.makedirs(output_dir, exist_ok=True)
json.dump(metrics, open(f"{output_dir}/metrics.json", "w"), indent=2)
print(json.dumps(metrics, indent=2))
