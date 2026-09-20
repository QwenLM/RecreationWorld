"""Score aggregator for RecreationBench Web.

Aggregates four-dimensional test results and optional VLM judge scores
into a final weighted score.
"""

import logging
from typing import Optional

from config import TEST_WEIGHTS
from evaluation.vlm_judge import VLMJudgeResult

logger = logging.getLogger(__name__)


def aggregate_scores(
    test_results: dict,
    vlm_result: Optional[VLMJudgeResult] = None,
    eval_config: dict = None,
    visual_score: Optional[float] = None,
    structural_score: Optional[float] = None,
    quality_score: Optional[float] = None,
    functional_score: Optional[float] = None,
    dimension_counts: Optional[dict] = None,
) -> dict:
    """Aggregate multi-dimensional test results into a final score.

    Args:
        test_results: {dimension: TestDimensionResult} from test_runner
        vlm_result: Optional VLM judge result
        eval_config: Evaluation configuration. Consulted ONLY for the vlm_judge
            sub-config; dimension weights are always taken from config.TEST_WEIGHTS
            (authoritative). Any eval_config["test_weights"] is intentionally ignored.
        visual_score: SSIM/LPIPS/Layout-IoU visual score (0.0-1.0) used instead of
            the Playwright pass rate for the visual dimension.
        structural_score: continuous DOM-similarity-blended score (0.0-1.0) used
            instead of the Playwright pass rate for the structural dimension.
        quality_score: continuous GT-relative quality-blended score (0.0-1.0) used
            instead of the Playwright pass rate for the quality dimension.
        dimension_counts: optional {dim: {"total","passed","failed","continuous"}} giving
            REAL unit counts for dimensions whose score is overridden by a continuous
            scorer. For functional these are the binary test pass/total; for the
            continuous dims (visual/structural/quality) they are coverage (units scored /
            units attempted) with ``continuous=True``. Used to populate dimension_details
            with honest counts instead of hardcoded 0/0 (P2-4).

    Returns:
        dict with final_score, test_score, breakdown, weights, vlm_score
    """
    # config.py TEST_WEIGHTS is the single authoritative weight source — per-task
    # eval_config["test_weights"] is intentionally ignored (it previously caused
    # score drift between dataset epochs; audit finding). eval_config is still
    # consulted for the VLM-judge sub-config only.
    weights = dict(TEST_WEIGHTS)
    vlm_config = (eval_config or {}).get("vlm_judge", {"enabled": False})

    # Compute per-dimension pass rates
    breakdown = {}
    for dim, weight in weights.items():
        result = test_results.get(dim)
        if result is None:
            breakdown[dim] = 0.0
        elif hasattr(result, "pass_rate"):
            breakdown[dim] = result.pass_rate
        elif isinstance(result, dict):
            breakdown[dim] = result.get("pass_rate", 0.0)
        else:
            breakdown[dim] = 0.0

    # Override dimensions that are scored outside the Playwright pass-rate with
    # their continuous scores: visual = SSIM/LPIPS/Layout-IoU; structural =
    # DOM-similarity blend; quality = GT-relative quality blend.
    _overrides = {
        "visual": (visual_score, "ssim"),
        "structural": (structural_score, "dom_similarity"),
        "quality": (quality_score, "qs_continuous"),
        "functional": (functional_score, "tier_weighted"),
    }
    for _dim, (_val, _method) in _overrides.items():
        if _val is not None and _dim in breakdown:
            breakdown[_dim] = _val

    # VLM judge feeds the VISUAL dimension. When enabled, the VLM score (GT-vs-
    # replica checklist / assertion pass-rate) REPLACES the SSIM/LPIPS visual
    # score in the four-dimension weighted sum — it is a smarter visual-fidelity
    # measure, not a separate layer bolted onto the final score.
    vlm_score = None
    vlm_available = bool(
        vlm_result and vlm_config.get("enabled", False) and vlm_result.score >= 0
    )
    if vlm_available:
        vlm_score = vlm_result.score
        if "visual" in breakdown:
            breakdown["visual"] = vlm_score

    # Weighted test score (computed AFTER the visual override above)
    total_weight = sum(weights.values())
    if total_weight > 0:
        test_score = sum(
            weights[dim] * breakdown.get(dim, 0.0)
            for dim in weights
        ) / total_weight
    else:
        test_score = 0.0

    final_score = test_score

    # Build detailed result
    dimension_details = {}
    counts = dimension_counts or {}
    for dim in weights:
        _ov = _overrides.get(dim)
        if _ov is not None and _ov[0] is not None:
            c = counts.get(dim) or {}
            dimension_details[dim] = {
                "pass_rate": _ov[0],
                "method": _ov[1],
                "total": c.get("total", 0),
                "passed": c.get("passed", 0),
                "failed": c.get("failed", 0),
                "continuous": c.get("continuous", True),
            }
            continue

        result = test_results.get(dim)
        if result is None:
            dimension_details[dim] = {
                "pass_rate": 0.0, "total": 0, "passed": 0, "failed": 0,
            }
        elif hasattr(result, "total"):
            dimension_details[dim] = {
                "pass_rate": result.pass_rate,
                "total": result.total,
                "passed": result.passed,
                "failed": result.failed,
                "compile_error": getattr(result, "compile_error", False),
            }
        elif isinstance(result, dict):
            dimension_details[dim] = result
        else:
            dimension_details[dim] = {"pass_rate": 0.0}

    # When the VLM judge is enabled, it provides the visual dimension — reflect
    # that in the per-dimension details so reports show the source. SSIM is still
    # always computed; keep it visible here (ssim_score) even though the VLM score
    # is what drives the weighted visual dimension.
    if vlm_available and "visual" in dimension_details:
        dimension_details["visual"] = {
            "pass_rate": vlm_score, "method": "vlm_judge",
            "total": 0, "passed": 0, "failed": 0,
            "ssim_score": round(visual_score, 4) if visual_score is not None else None,
        }

    return {
        "final_score": round(final_score, 4),
        "test_score": round(test_score, 4),
        "breakdown": {k: round(v, 4) for k, v in breakdown.items()},
        "weights": weights,
        "dimension_details": dimension_details,
        "has_compile_errors": any(
            isinstance(d, dict) and d.get("compile_error") for d in dimension_details.values()
        ),
        "vlm_score": round(vlm_score, 4) if vlm_score is not None else None,
        "vlm_enabled": vlm_config.get("enabled", False),
        # Keep the scoring mode in every result so a release pin can be audited.
        # release_web.yaml explicitly selects comparison today; local configs that
        # omit the field use the assertion default from config.py.
        "vlm_mode": vlm_config.get("mode") or "assertion",
        # SSIM/LPIPS visual score — ALWAYS computed and surfaced for reference, even
        # when the VLM judge drives the weighted visual dimension (vlm_score replaces
        # it in the sum). null only if the SSIM computation was unavailable.
        "visual_ssim_score": round(visual_score, 4) if visual_score is not None else None,
    }


def format_score_report(scores: dict) -> str:
    """Format score dict as a human-readable report string."""
    lines = [
        f"Final Score: {scores['final_score']:.4f}",
        f"Test Score:  {scores['test_score']:.4f}",
        "",
        "Dimension Breakdown:",
    ]

    weights = scores.get("weights", {})
    breakdown = scores.get("breakdown", {})
    details = scores.get("dimension_details", {})

    for dim in ["functional", "visual", "structural", "quality"]:
        w = weights.get(dim, 0)
        rate = breakdown.get(dim, 0)
        d = details.get(dim, {})
        passed = d.get("passed", 0)
        total = d.get("total", 0)
        lines.append(
            f"  {dim:12s}: {rate:.2%} ({passed}/{total}) "
            f"[weight: {w:.0%}]"
        )

    subs = scores.get("functional_subscores") or {}
    if subs:
        lines.append("")
        lines.append("Functional sub-scores (per spec file, diagnostic):")
        for key in sorted(subs):
            s = subs[key]
            lines.append(
                f"  {key:32s}: {s.get('pass_rate', 0):.2%} "
                f"({s.get('passed', 0)}/{s.get('total', 0)}) "
                f"[weighted {s.get('weighted', 0):.2%}]"
            )

    if scores.get("vlm_enabled"):
        vlm = scores.get("vlm_score")
        if vlm is not None:
            lines.append(f"\nVLM Judge: {vlm:.4f}")
        else:
            # enabled in config but produced no usable score (abstained / creds
            # missing / empty assertions) — visual fell back to SSIM.
            lines.append("\nVLM Judge: enabled but unavailable "
                         "(visual fell back to SSIM)")

    return "\n".join(lines)
