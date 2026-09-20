#!/usr/bin/env python3
"""Unified deployment platform metrics.json contract — the single writer for all 5 platforms.

Maps every platform pipeline's result into one task_score/program_score/
vlm_score/passed contract. Platform-specific runtime and scoring remain behind
the platform boundary.

Design notes:
  * `eval_scores()` normalizes the two eval-result shapes into one:
      - linux/mac: separate programmatic_results.json{passed,total,pass_rate}
        + vlm_results.json{...}
      - windows:   a combined per-stage eval dict
        {programmatic_pass_rate, programmatic_total, vlm_pass_rate, vlm_total,
         vlm_judge_errors}
  * `select_scoring_eval()` selects the release ``eval`` result.
  * `classify_outcome()` is the windows infra/stage exit taxonomy, promoted so
    every platform emits the same rich exit_code/exit_reason (linux only had a
    bare passed bool before).
  * `pass_rule` selects the `passed` semantics WITHOUT silently changing scores:
    "exit_and_stages" (linux's rule) is the default; other rules are opt-in and
    must be validated by real A/B runs before a platform switches.

Score SEMANTICS unification (one task_score formula over the frozen manifest per
core.manifest) is intentionally NOT forced here — it changes real numbers and is
gated on real-run verification. This module first makes the CODE one copy.
"""

from __future__ import annotations

from typing import Any, Optional

from core import exit_contract

DEFAULT_EVAL_PRIORITY = ("eval",)


def _rate(d: Optional[dict], *keys: str) -> float:
    """First present numeric key in d, else 0.0. Tolerates both schemas."""
    if not isinstance(d, dict):
        return 0.0
    for k in keys:
        if k in d and d[k] is not None:
            try:
                return float(d[k])
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _count(d: Optional[dict], *keys: str) -> int:
    if not isinstance(d, dict):
        return 0
    for k in keys:
        if k in d and d[k] is not None:
            try:
                return int(d[k])
            except (TypeError, ValueError):
                return 0
    return 0


def eval_scores(
    *,
    programmatic: Optional[dict] = None,
    vlm: Optional[dict] = None,
    combined: Optional[dict] = None,
) -> dict:
    """Normalize either eval-result shape into a common score dict.

    linux/mac pass ``programmatic=<programmatic_results.json>`` and
    ``vlm=<vlm_results.json>``; windows passes ``combined=<stage eval dict>``.
    Returns ``{program_score, program_n, vlm_score, vlm_n, vlm_err}``.
    """
    if combined is not None:
        return {
            "program_score": _rate(combined, "programmatic_pass_rate", "pass_rate"),
            "program_n": _count(combined, "programmatic_total", "total"),
            "vlm_score": _rate(combined, "vlm_pass_rate"),
            "vlm_n": _count(combined, "vlm_total"),
            "vlm_err": _count(combined, "vlm_judge_errors"),
        }
    return {
        "program_score": _rate(programmatic, "pass_rate", "programmatic_pass_rate"),
        "program_n": _count(programmatic, "total", "programmatic_total"),
        "vlm_score": _rate(vlm, "pass_rate", "vlm_pass_rate"),
        "vlm_n": _count(vlm, "total", "vlm_total"),
        "vlm_err": _count(vlm, "vlm_judge_errors", "errors"),
    }


# Android writes `ok` for a passed stage; desktop platforms write `pass`. Normalize
# both at the shared boundary so every comparison uses the same contract.
# `below_gate`, `no_testcases`, and `failed` deliberately remain non-passing.
_STAGE_PASS_ALIASES = frozenset({"pass", "ok"})


def normalize_stages(stages: Optional[dict]) -> dict:
    """Map each platform's success word onto the canonical ``pass``."""
    return {
        k: ("pass" if v in _STAGE_PASS_ALIASES else v)
        for k, v in (stages or {}).items()
    }


def select_scoring_eval(
    stages: dict,
    evals: dict,
    stage: Optional[str] = None,
    priority: tuple[str, ...] = DEFAULT_EVAL_PRIORITY,
) -> tuple[Optional[str], Optional[dict]]:
    """Pick the eval whose programmatic score becomes task_score.

    Prefer the final stage selected by the canonical ``stage`` token, then the
    release ``eval``.
    The single-result fallback supports platform-native result names without
    reintroducing retired lifecycle stages.
    """
    stages = normalize_stages(stages)
    evals = evals or {}
    selected = "eval" if stage == "recreation_eval" else stage
    if selected and stages.get(selected) == "pass" and evals.get(selected):
        return selected, evals[selected]
    for name in priority:
        if stages.get(name) == "pass" and evals.get(name):
            return name, evals[name]
    present = [(k, v) for k, v in evals.items() if v]
    if len(present) == 1:
        return present[0]
    return None, None


def classify_outcome(
    pipeline_exit: int,
    stages: dict,
    summary: Optional[dict],
    sandbox_info: Optional[dict] = None,
    *,
    pass_rule: str = "exit_and_stages",
    required_stages: Optional[list[str]] = None,
) -> dict:
    """Return {exit_code, exit_reason, error, passed} — windows taxonomy for all.

    pass_rule:
      "exit_and_stages" (default): passed = exit0 AND all required_stages pass
                                   (linux's rule; None required_stages -> exit0).
      "exit_only":                 passed = (pipeline_exit == 0) (windows legacy).
    """
    stages = normalize_stages(stages)
    stages = stages or {}
    sandbox_info = sandbox_info or {}
    out: dict[str, Any] = {}

    infra_error = sandbox_info.get("error")
    if infra_error:
        return {
            "exit_code": "infra:sandbox_error",
            "exit_reason": str(infra_error)[:500],
            "error": f"Sandbox error: {str(infra_error)[:200]}",
            "passed": False,
        }
    if summary is None and pass_rule != "exit_only":
        # linux uses pipeline_state, not summary; only flag no-summary when a
        # caller that relies on summary passes it as None.
        pass

    # passed rule
    if pass_rule == "exit_only":
        passed = pipeline_exit == 0
    else:
        req = required_stages or []
        passed = pipeline_exit == 0 and all(stages.get(name) == "pass" for name in req)

    if passed:
        out["passed"] = True
        return out

    # non-pass: classify the failing stage (windows taxonomy)
    out["passed"] = False
    failed_stage = None
    failed_status = None
    for s, st in stages.items():
        if s.endswith("_fail_code"):
            continue
        if st != "pass":
            failed_stage = s
            failed_status = st
            break
    failure_hint = ((summary or {}).get("failure_hint") or "")[:500]
    fail_code = stages.get(f"{failed_stage}_fail_code", "") if failed_stage else ""

    _INFRA_CODES = {
        "no_recreation_exe": ("stage:eval_no_recreation_exe", "no recreation exe"),
        "vlm_judge_errors": ("infra:eval_vlm_judge_errors", "vlm judge errors"),
        "artifacts_missing": (
            "infra:{s}_artifacts_missing",
            "artifacts missing",
        ),
        "pipeline_crashed": ("infra:{s}_pipeline_crashed", "pipeline process crashed"),
        "rebuild_failed": ("infra:{s}_rebuild_failed", "rebuild failed"),
        "pipeline_not_started": (
            "infra:{s}_pipeline_not_started",
            "pipeline not started",
        ),
        "crash": ("infra:{s}_crash", "crashed"),
    }
    if failed_stage and failed_status == "timeout":
        out["exit_code"] = f"infra:{failed_stage}_timeout"
        out["error"] = f"{failed_stage} timed out"
    elif failed_stage and fail_code in _INFRA_CODES:
        code, msg = _INFRA_CODES[fail_code]
        out["exit_code"] = code.format(s=failed_stage)
        out["error"] = f"{failed_stage}: {msg}"
    elif failed_stage:
        out["exit_code"] = f"stage:{failed_stage}_failed"
        out["error"] = f"{failed_stage} failed"
    else:
        out["exit_code"] = f"infra:exit_{pipeline_exit}"
        out["error"] = f"Unexpected exit code {pipeline_exit}"
    out["exit_reason"] = failure_hint or out["error"]
    return out


def build_metrics(
    *,
    task_id: str,
    model: str,
    pipeline_exit: int,
    stages: dict,
    evals: Optional[dict] = None,
    sandbox_info: Optional[dict] = None,
    summary: Optional[dict] = None,
    stage: Optional[str] = None,
    pass_rule: str = "exit_and_stages",
    required_stages: Optional[list[str]] = None,
    eval_priority: tuple[str, ...] = DEFAULT_EVAL_PRIORITY,
) -> dict:
    """Assemble the unified metrics.json dict emitted to deployment platform by every platform.

    ``evals`` maps stage-name -> that stage's normalized score dict (from
    ``eval_scores``). ``task_score`` = the scoring eval's program_score selected
    by ``select_scoring_eval``. Emits program_score/vlm_score (which linux
    previously omitted) plus the exit taxonomy (which linux previously lacked).
    """
    sandbox_info = sandbox_info or {}
    evals = evals or {}
    metrics: dict[str, Any] = {
        "task_id": task_id,
        "model": model,
        "sandbox_id": sandbox_info.get("sandbox_id"),
        "sandbox_ip": sandbox_info.get("sandbox_ip"),
        "pipeline_exit_code": pipeline_exit,
    }
    if stage is not None:
        metrics["stage"] = stage

    # per-stage prog/vlm fields (windows-style {prefix}_prog/_vlm) for all
    for name, sc in evals.items():
        if not isinstance(sc, dict):
            continue
        metrics[f"{name}_prog"] = sc.get("program_score", 0)
        metrics[f"{name}_prog_n"] = sc.get("program_n", 0)
        metrics[f"{name}_vlm"] = sc.get("vlm_score", 0)
        metrics[f"{name}_vlm_n"] = sc.get("vlm_n", 0)
        metrics[f"{name}_vlm_err"] = sc.get("vlm_err", 0)

    sel_name, sel = select_scoring_eval(stages, evals, stage, eval_priority)
    # "Was this run asked to score a recreation at all?" is the distinction that matters, and
    # it is NOT the same as "did an eval produce a score":
    #   * a build failure means the recreation could not be built. That is a real 0 the model
    #     earned, and dropping it from the denominator would flatter a model that fails to
    #     build half its apps.
    #   * a setup-only diagnostic was never asked to judge a recreation, so
    #     there is nothing to score and 0 would be a score the model never earned.
    # A present score is always kept, so a platform whose eval dict omits the count is never
    # nulled.
    # setup is a standalone permission diagnostic, not an eval stage.  Keep that
    # distinction local instead of folding setup into the legacy eval-name list.
    _scores_requested = stage != "setup"
    _prog = sel.get("program_score") if sel else None
    if _prog is None:
        _prog = 0.0 if (_scores_requested or (sel and sel.get("program_n"))) else None
    metrics["task_score"] = _prog
    metrics["program_score"] = _prog
    # An UNGRADED vlm dimension is None, never 0. `vlm_n` is the number of assertions the
    # judge actually graded: 0 means it never ran (web's judge abstains with a -1 sentinel
    # when it has no assertions and the visual dimension falls back to SSIM/LPIPS). Writing
    # 0 there is a real score the model never earned, and prog_vlm_avg would then
    # divide the programmatic score by 2.
    metrics["vlm_score"] = (
        (sel.get("vlm_score", 0) or 0) if (sel and sel.get("vlm_n")) else None
    )
    metrics["scoring_eval"] = sel_name
    if not _scores_requested and metrics["task_score"] is None:
        # The range never included a recreation eval, so this task has no score to
        # contribute. Marked rather than left implicit, so aggregation skips it instead of
        # counting it as 0 — or, before the selection fix above, as the REFERENCE's score.
        metrics["excluded_from_scoring"] = True
        metrics["score_absent_reason"] = (
            f"stage {stage!r} produced no recreation eval result"
        )

    outcome = classify_outcome(
        pipeline_exit,
        stages,
        summary,
        sandbox_info,
        pass_rule=pass_rule,
        required_stages=required_stages,
    )
    metrics.update(outcome)
    # normalize stage status strings (windows emitted succeeded/failed labels)
    for s, st in (stages or {}).items():
        if s.endswith("_fail_code"):
            continue
        metrics.setdefault(f"stage_{s}", "succeeded" if st == "pass" else "failed")
    return metrics


def assemble_metrics(
    state: dict,
    *,
    platform: str,
    pass_rule: str = "exit_and_stages",
    required_stages: Optional[list[str]] = None,
    stage: Optional[str] = None,
    sandbox_info: Optional[dict] = None,
) -> dict:
    """The single state -> unified metrics.json terminus for ALL 5 platforms.

    A platform pipeline maps its result into ``state`` and the shared entry
    calls this. It does NOT recompute scores: task_score/program_score/
    vlm_score that the platform set on ``state`` (android macro-average, web 4-dim
    weighted, ...) are honored as OVERRIDES on top of build_metrics' generic
    assembly, so each platform's mandatory scoring is preserved while the emitted
    JSON shape is identical everywhere.
    """
    metrics = build_metrics(
        task_id=state.get("task_id", ""),
        model=state.get("model", ""),
        pipeline_exit=int(state.get("pipeline_exit", 0)),
        stages=state.get("stages", {}) or {},
        evals=state.get("evals", {}) or {},
        sandbox_info=sandbox_info,
        summary=state.get("summary"),
        stage=stage,
        pass_rule=pass_rule,
        required_stages=list(required_stages or ()),
    )
    for k in ("task_score", "program_score", "vlm_score"):
        if state.get(k) is not None:
            metrics[k] = state[k]
    # Precomputed per-task prog/vlm average as a TOP-LEVEL field, so the deployment platform's
    # A single-field external aggregation can produce the group-level
    # macro_prog_vlm_avg (it can only avg/sum ONE field, not a derived combination), and
    # every metrics.json carries it uniformly alongside program_score/vlm_score.
    _dims = [
        v
        for v in (metrics.get("program_score"), metrics.get("vlm_score"))
        if v is not None
    ]
    metrics["prog_vlm_avg"] = round(sum(_dims) / len(_dims), 6) if _dims else None
    # An explicit passed on the state is an authoritative PIPELINE-health verdict.  Score
    # thresholds are kept separately as score_passed so a low-quality but fully evaluated
    # result does not turn the deployment platform job into an infrastructure failure.
    if state.get("passed") is not None:
        metrics["passed"] = bool(state["passed"])
    if state.get("score_passed") is not None:
        metrics["score_passed"] = bool(state["score_passed"])
    # Explicitly unscored work (for example setup-only or an infra failure before any
    # model attempt): carry the marker through and null every score.
    if state.get("excluded_from_scoring"):
        metrics["excluded_from_scoring"] = True
        metrics["exclusion_reason"] = state.get("exclusion_reason")
        # EVERY dimension goes null, not just task_score: an excluded task has no
        # measurements at all. program_score kept build_metrics' 0 default here, so a scan
        # averaging that field directly would still pick up a zero the run never earned.
        metrics["task_score"] = None
        metrics["program_score"] = None
        metrics["vlm_score"] = None
        metrics["prog_vlm_avg"] = (
            None  # drop from the deployment platform field-avg denominator too
        )
        metrics["passed"] = bool(state.get("passed", False))
    metrics["platform"] = platform
    native_exit = state.get("native_exit_code")
    if native_exit is not None:
        metrics["native_exit_code"] = native_exit
    if state.get("stage_outcomes") is not None:
        metrics["stage_outcomes"] = state["stage_outcomes"]
    # Process status is selected once, here, after platform-native evidence and
    # score/exclusion overrides have all landed.  It is intentionally independent
    # of ``passed``: a completed low/zero result may be passed=False and still be a
    # terminal, non-retryable process success.
    public = exit_contract.describe(state, metrics, requested_stage=stage)
    raw_pipeline_exit = metrics.get("pipeline_exit_code")
    metrics.update(public)
    # Both names are public lifecycle fields and therefore must agree. Preserve
    # a legacy/private worker value only as diagnostic evidence.
    if raw_pipeline_exit != public["process_exit_code"]:
        metrics.setdefault("native_exit_code", raw_pipeline_exit)
    metrics["pipeline_exit_code"] = public["process_exit_code"]
    return metrics
