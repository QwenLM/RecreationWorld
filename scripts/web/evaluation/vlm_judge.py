"""Web aggregation adapter for the shared five-platform VLM judge.

Unified assertion-based evaluation: both comparison (GT vs agent) and text
assertion modes produce pass/fail verdicts. Score = pass_rate.
The prompt, image encoding, API call, retry policy, and parsing live in
``common.vlm_judge``; this module only maps web pages/viewports into that contract.
"""

import asyncio
import logging
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from common import vlm_judge as shared_vlm_judge

logger = logging.getLogger(__name__)

# ── Comparison fallback assertions ───────────────────────────────────
# Used only when a site has no generated vlm_checklist for a page/viewport.

COMPARISON_ASSERTIONS = [
    # layout
    "The header and navigation bar in the replica match the reference "
    "in position, height, and internal arrangement.",
    "The main content area structure (number of columns, content block "
    "placement) in the replica matches the reference.",
    "The sidebar, if present in the reference, appears in the replica "
    "at the same side with a similar width.",
    "The footer in the replica matches the reference in position and "
    "internal layout.",
    # typography
    "All heading text content (h1, h2, h3, etc.) in the replica matches "
    "the reference.",
    "The heading visual hierarchy (relative sizes and font weights) in "
    "the replica is consistent with the reference.",
    "Body and paragraph text content in the replica is fully present "
    "and matches the reference.",
    "Font families and font sizes used in the replica are visually "
    "consistent with the reference.",
    # color and style
    "Background colors of each section in the replica match the reference.",
    "Text colors (headings, body, links) in the replica match the reference.",
    "Accent colors used for buttons, tags, and highlighted elements in "
    "the replica match the reference.",
    "Border styles, box shadows, and rounded corners in the replica "
    "match the reference.",
    # images and media
    "All images and photographs visible in the reference are present "
    "in the replica at correct positions and similar sizes.",
    "Icons and logos visible in the reference are present and correctly "
    "placed in the replica.",
    # interactive elements
    "Button appearance (shape, color, size, label) in the replica "
    "matches the reference.",
    "Form controls (input fields, dropdowns, checkboxes) in the replica "
    "match the reference in appearance.",
    "Navigation links and menu items in the replica are styled "
    "consistently with the reference.",
    # spacing and alignment
    "Vertical spacing between major sections in the replica is "
    "consistent with the reference.",
    "Horizontal margins and padding around content blocks in the "
    "replica match the reference.",
    "Text and element alignment (left, center, right) in the replica "
    "matches the reference.",
]

# ── Data structures ──────────────────────────────────────────────────


@dataclass
class VLMJudgeResult:
    """VLM Judge evaluation result."""

    score: float
    page_scores: dict[str, float] = field(default_factory=dict)
    page_details: dict[str, dict] = field(default_factory=dict)
    reasoning: str = ""


class VLMJudge(ABC):
    """Abstract base class for VLM judges."""

    @abstractmethod
    async def judge(
        self,
        gt_screenshots: dict[str, list[str]],
        agent_screenshots: dict[str, list[str]],
        task_context: dict,
    ) -> VLMJudgeResult: ...


class PlaceholderVLMJudge(VLMJudge):
    """Returns negative score to signal disabled."""

    async def judge(self, gt_screenshots, agent_screenshots, task_context):
        return VLMJudgeResult(score=-1.0)


def _verdict_score(v: dict) -> float:
    """Extract a 0..1 score from a verdict. Supports graded ('score') and legacy
    binary ('pass') shapes; unparseable verdicts count as 0."""
    if isinstance(v, dict) and v.get("score") is not None:
        try:
            return max(0.0, min(1.0, float(v["score"])))
        except (TypeError, ValueError):
            return 0.0
    # Accept the model's `pass` as a boolean, or a truthy string/number it may
    # emit instead (e.g. "true"/"yes"/"pass"/1). Using `is True` alone would
    # silently score an intended-pass written as "pass":"true" as FAIL — a
    # one-directional bias that only ever lowers the score.
    p = v.get("pass") if isinstance(v, dict) else None
    if (
        p is True
        or p == 1
        or (isinstance(p, str) and p.strip().lower() in ("true", "yes", "pass", "1"))
    ):
        return 1.0
    return 0.0


# ── Unified Assertion Judge ──────────────────────────────────────────


class AssertionVLMJudge(VLMJudge):
    """Unified assertion-based VLM judge.

    Handles both modes:
    - comparison: Shows GT + agent screenshots, evaluates COMPARISON_ASSERTIONS
    - assertion: Shows only agent screenshots, evaluates custom assertions
      from task_context["vlm_assertions"]

    Both produce pass/fail verdicts. Score = pass_rate.
    """

    def __init__(
        self,
        mode: str,
        backend: str = "openai-compatible",
        model: str = "",
        api_key: str = None,
        base_url: str = None,
        max_concurrency: int = 8,
    ):
        self.mode = mode
        # ``backend`` is retained as metadata compatibility for older frozen Web
        # configurations. The transport is always the shared OpenAI-compatible client.
        self.backend = backend or "openai-compatible"
        self.model = model or shared_vlm_judge.DEFAULT_MODEL
        self.max_concurrency = max_concurrency
        self._sem = None
        self.cfg = shared_vlm_judge.resolve(
            model=self.model, base_url=base_url or "", api_key=api_key or ""
        )
        self.api_key = self.cfg["api_key"]
        self.base_url = self.cfg["base_url"]

    async def judge(
        self,
        gt_screenshots,
        agent_screenshots,
        task_context,
        prior_details=None,
        on_progress=None,
    ):
        """Judge all views.

        prior_details: page_details from a previous run; completed views are
            seeded and skipped (resume / 续测). Failed/error/missing views are
            re-judged.
        on_progress: optional callable(page_scores, page_details) invoked after
            each view completes, for incremental checkpointing to disk.
        """
        self._sem = asyncio.Semaphore(self.max_concurrency)
        self._prior = prior_details or {}
        self._on_progress = on_progress

        if self.mode == "assertion":
            return await self._judge_assertion_mode(
                agent_screenshots,
                task_context,
            )
        else:
            return await self._judge_comparison_mode(
                gt_screenshots,
                agent_screenshots,
                task_context,
            )

    @staticmethod
    def _pick_viewport(paths, viewport):
        """Pick the screenshot path matching a viewport (by filename) from a list."""
        for p in paths or []:
            if Path(p).name.replace(".png", "") == viewport and Path(p).exists():
                return p
        return None

    @staticmethod
    def _norm_item(it):
        """Normalize one assertion entry to {'text', 'weight'}.

        Accepts either a plain string (weight 1.0 — legacy format) or a dict
        carrying a per-assertion weight, e.g. {'text'/'assertion': str,
        'weight': float}. The weight lets the discriminative, page-specific
        assertions count more than generic shared-chrome ones when the visual
        score is aggregated (see _aggregate_viewport_jobs)."""
        if isinstance(it, dict):
            text = it.get("text", it.get("assertion", ""))
            try:
                w = float(it.get("weight", 1.0))
            except (TypeError, ValueError):
                w = 1.0
            return {"text": str(text), "weight": w}
        return {"text": str(it), "weight": 1.0}

    @staticmethod
    def _group_by_page_viewport(assertions_map):
        """{(page, viewport): [{'text','weight'}]} from a 'page/viewport.png' -> [items] map.

        Items may be plain strings (legacy, weight 1.0) or {'text'/'assertion',
        'weight'} dicts; both are normalized here."""
        from collections import defaultdict

        by_pv = defaultdict(list)
        for key, items in assertions_map.items():
            if isinstance(items, (str, dict)):
                items = [items]
            parts = key.replace("\\", "/").split("/")
            page = parts[0] if parts else key
            vp = parts[1].replace(".png", "") if len(parts) > 1 else "desktop"
            by_pv[(page, vp)].extend(AssertionVLMJudge._norm_item(it) for it in items)
        return by_pv

    async def _aggregate_viewport_jobs(self, jobs, zeros, mode_label):
        """Run per-(page,viewport) judging jobs, grade each item 0..1, then combine
        viewports per page using RESOLUTION_WEIGHTS (desktop 0.6 / mobile 0.4),
        mirroring visual_score. `zeros` are (page, viewport) with a GT item set but
        no agent screenshot — scored 0 (missing content).

        Judge FAILURES (request error, or a view that produced no usable verdicts)
        ABSTAIN (score=None) instead of scoring 0: an abstained view is excluded from
        the page/overall average, and if the whole run produces no usable verdict at
        all the overall score is a negative sentinel (-1.0) so the caller falls back
        to the SSIM/LPIPS visual score rather than a spurious 0. Legit "no agent
        screenshot" zeros are real content misses and still count as 0.

        Views from a prior run (``self._prior``) are seeded first so resumed runs
        carry completed views forward. After each view finishes, ``self._on_progress``
        (if set) receives a fresh ``(page_scores, page_details)`` snapshot for
        incremental checkpointing."""
        from collections import defaultdict

        try:
            import config as web_config

            resolution_weights = web_config.RESOLUTION_WEIGHTS
        except Exception:
            resolution_weights = {"desktop": 0.6, "mobile": 0.4}

        vp_data = defaultdict(dict)  # page -> {vp: {score, verdicts/error}}
        # Seed completed views from a prior run (resume support).
        for page, e in (getattr(self, "_prior", None) or {}).items():
            for vp, dd in (e.get("per_viewport") or {}).items():
                vp_data[page][vp] = dd

        def _page_score(vps):
            # Only viewports with a real (non-None) score count; abstained
            # (judge-failure) viewports are excluded. Returns None if the whole
            # page abstained.
            real = {vp: d for vp, d in vps.items() if d.get("score") is not None}
            if not real:
                return None
            tw = sum(resolution_weights.get(vp, 0.0) for vp in real)
            if tw > 0:
                return round(
                    sum(
                        d["score"] * resolution_weights.get(vp, 0.0)
                        for vp, d in real.items()
                    )
                    / tw,
                    4,
                )
            return round(sum(d["score"] for d in real.values()) / len(real), 4)

        def _build():
            page_scores = {p: _page_score(vps) for p, vps in vp_data.items()}
            page_details = {
                p: {"score": page_scores[p], "per_viewport": dict(vps)}
                for p, vps in vp_data.items()
            }
            return page_scores, page_details

        def _emit():
            cb = getattr(self, "_on_progress", None)
            if cb:
                try:
                    cb(*_build())
                except Exception as e:
                    logger.warning("on_progress checkpoint failed: %s", e)

        for page, vp in zeros:
            logger.warning(
                "No agent screenshot for %s/%s — scoring this view 0 "
                "(missing content).",
                page,
                vp,
            )
            vp_data[page][vp] = {"score": 0.0, "note": "no agent screenshot"}
        if zeros:
            _emit()

        async def _run(job):
            try:
                res = await self._evaluate_assertions(
                    image_paths=job["images"],
                    assertions=job["items"],
                    comparison=bool(job.get("gt")),
                )
                return job, res, None
            except Exception as e:
                return job, None, e

        for fut in asyncio.as_completed([asyncio.ensure_future(_run(j)) for j in jobs]):
            job, res, err = await fut
            page, vp, items = job["page"], job["viewport"], job["items"]
            weights = job.get("weights") or [1.0] * len(items)
            if err is not None:
                # Judge errored on this view — abstain (score=None), do NOT score 0.
                logger.warning(
                    "VLM judge failed %s/%s: %s — abstaining this view "
                    "(excluded from average).",
                    page,
                    vp,
                    err,
                )
                vp_data[page][vp] = {
                    "score": None,
                    "error": str(err),
                    "abstained": True,
                }
                _emit()
                continue
            # All-None verdicts mean the judge never actually evaluated this view
            # (request failed / unparseable) — that is NOT a real 0, so abstain
            # (score=None) instead of silently scoring it 0.
            graded = [
                v
                for v in res
                if isinstance(v, dict)
                and (v.get("score") is not None or v.get("pass") is not None)
            ]
            if items and not graded:
                logger.warning(
                    "VLM judge produced no usable verdicts for %s/%s "
                    "(%d items) — abstaining this view (excluded from "
                    "average); treat as judge error, not a real zero.",
                    page,
                    vp,
                    len(items),
                )
                vp_data[page][vp] = {
                    "score": None,
                    "total": len(items),
                    "failed": True,
                    "abstained": True,
                    "verdicts": [
                        {"item": a, **(v if isinstance(v, dict) else {})}
                        for a, v in zip(items, res)
                    ],
                }
                _emit()
                continue
            if graded and len(res) != len(items):
                logger.warning(
                    "VLM judge returned %d verdicts for %d items on "
                    "%s/%s — count mismatch; only the overlapping "
                    "items are scored.",
                    len(res),
                    len(items),
                    page,
                    vp,
                )
            scores = [_verdict_score(v) for v in res]
            # Weighted pass-rate: discriminative, page-specific assertions carry
            # more weight than generic shared-chrome ones (weights come from the
            # generator's discrimination pass; legacy string assertions are all
            # 1.0, so this reduces to the plain mean). Only overlapping
            # (weight, score) pairs count on a count mismatch.
            pairs = list(zip(weights, scores))
            wsum = sum(w for w, _ in pairs)
            verdicts = [
                {"item": a, "weight": w, **(v if isinstance(v, dict) else {})}
                for a, w, v in zip(items, weights, res)
            ]
            if wsum > 0:
                vp_score = sum(w * s for w, s in pairs) / wsum
                vp_data[page][vp] = {
                    "score": round(vp_score, 4),
                    "total": len(items),
                    "verdicts": verdicts,
                }
            else:
                # Every assertion here has weight 0 (all failed their own GT in the
                # weighting pass) → no reliable signal. Abstain (excluded from the
                # average) rather than averaging assertions the weighting discarded.
                # Legacy all-1.0 sets never reach here (wsum == len > 0).
                logger.warning(
                    "all-zero weights for %s/%s (%d items) — abstaining "
                    "this view (excluded from average).",
                    page,
                    vp,
                    len(items),
                )
                vp_data[page][vp] = {
                    "score": None,
                    "total": len(items),
                    "abstained": True,
                    "verdicts": verdicts,
                }
            _emit()

        page_scores, page_details = _build()
        # Average over pages that produced a real score; if every page abstained
        # (judge never returned a usable verdict), signal "unavailable" with a
        # negative sentinel so aggregate_scores falls back to the SSIM visual score.
        real_page_scores = [s for s in page_scores.values() if s is not None]
        overall = (
            sum(real_page_scores) / len(real_page_scores) if real_page_scores else -1.0
        )
        return VLMJudgeResult(
            score=overall,
            page_scores=page_scores,
            page_details=page_details,
            reasoning=f"{mode_label} ({self.backend}/{self.model})",
        )

    @staticmethod
    def _view_done(dd) -> bool:
        """Whether a prior-run view is a completed success that resume can skip
        (failures/errors/missing-verdicts are re-judged)."""
        if not isinstance(dd, dict):
            return False
        if dd.get("error") or dd.get("failed"):
            return False
        vs = dd.get("verdicts")
        if vs and all(
            str(v.get("reason")) in ("request failed", "parse error") for v in vs
        ):
            return False
        return bool(vs) or dd.get("note") == "no agent screenshot"

    def _skip_view(self, page, vp) -> bool:
        """True if (page, vp) is already completed in the seeded prior run."""
        prior = getattr(self, "_prior", None) or {}
        dd = ((prior.get(page) or {}).get("per_viewport") or {}).get(vp)
        return self._view_done(dd)

    # ── Comparison / checklist mode ──────────────────────────────────

    async def _judge_comparison_mode(
        self, gt_screenshots, agent_screenshots, task_context=None
    ):
        try:
            from config import RESOLUTION_WEIGHTS

            viewports = list(RESOLUTION_WEIGHTS.keys())
        except Exception:
            viewports = ["desktop", "mobile"]

        custom = (task_context or {}).get("vlm_checklist", {})
        by_pv = self._group_by_page_viewport(custom)
        if not custom:
            logger.warning(
                "No vlm_checklist provided — falling back to the generic "
                "%d-item comparison checklist for ALL pages. This is usually a "
                "misconfiguration (missing/empty vlm_checklist.json).",
                len(COMPARISON_ASSERTIONS),
            )

        jobs, zeros = [], []
        for page, gt_paths in gt_screenshots.items():
            for vp in viewports:
                gt = self._pick_viewport(gt_paths, vp)
                if not gt:
                    continue  # no GT for this viewport → skip (no penalty)
                if self._skip_view(page, vp):
                    continue  # completed in a prior run (resume) — seeded already
                items = by_pv.get((page, vp))
                if not items:
                    if (
                        custom
                    ):  # only noisy when a checklist exists but misses this page/vp
                        logger.warning(
                            "No vlm_checklist for %s/%s — falling back to the "
                            "generic comparison checklist for this page.",
                            page,
                            vp,
                        )
                    items = [self._norm_item(a) for a in COMPARISON_ASSERTIONS]
                ag = self._pick_viewport(agent_screenshots.get(page, []), vp)
                if not ag:
                    zeros.append((page, vp))
                    continue
                jobs.append(
                    {
                        "page": page,
                        "viewport": vp,
                        "images": [gt, ag],
                        "items": [it["text"] for it in items],
                        "weights": [it["weight"] for it in items],
                        "gt": True,
                    }
                )

        return await self._aggregate_viewport_jobs(jobs, zeros, "Checklist")

    # ── Assertion mode ───────────────────────────────────────────────

    async def _judge_assertion_mode(self, agent_screenshots, task_context):
        assertions_map = task_context.get("vlm_assertions", {})
        if not assertions_map:
            logger.warning("No VLM assertions provided")
            return VLMJudgeResult(score=-1.0)

        by_pv = self._group_by_page_viewport(assertions_map)
        jobs, zeros = [], []
        for (page, vp), items in by_pv.items():
            if self._skip_view(page, vp):
                continue  # completed in a prior run (resume) — seeded already
            ag = self._pick_viewport(agent_screenshots.get(page, []), vp)
            if not ag:
                zeros.append((page, vp))  # GT had this viewport, agent missing → 0
                continue
            jobs.append(
                {
                    "page": page,
                    "viewport": vp,
                    "images": [ag],
                    "items": [it["text"] for it in items],
                    "weights": [it["weight"] for it in items],
                }
            )

        return await self._aggregate_viewport_jobs(jobs, zeros, "Text assertions")

    # ── Core evaluation (shared by both modes) ───────────────────────

    async def _evaluate_assertions(
        self,
        image_paths: list[str],
        assertions: list[str],
        comparison: bool,
    ) -> list[dict]:
        """Adapt an async web viewport job to the shared synchronous judge."""
        sem = self._sem
        if sem is None:
            sem = self._sem = asyncio.Semaphore(self.max_concurrency)
        async with sem:
            verdicts = []
            batch_size = (
                shared_vlm_judge.DEFAULT_BATCH_SIZE
                if os.environ.get("RB_EVAL_TARGET", "").strip().lower()
                == "reference"
                else max(1, len(assertions))
            )
            for offset in range(0, len(assertions), batch_size):
                # No retries= here: a local pin overrode DEFAULT_RETRIES and kept the
                # 429 backoff from ever applying to web.  Keep each response within the
                # shared judge's supported verdict count; a large page can carry dozens
                # of assertions and one truncated reply must not turn the tail into
                # fabricated failures.
                verdicts.extend(
                    await shared_vlm_judge.judge_assertions_async(
                        image_paths,
                        assertions[offset : offset + batch_size],
                        comparison=comparison,
                        cfg=self.cfg,
                    )
                )
            return verdicts


# ── Factory ──────────────────────────────────────────────────────────


def create_vlm_judge(eval_config: dict) -> VLMJudge:
    """Create the appropriate VLM judge from eval config.

    Config fields (under "vlm_judge"):
        enabled: bool
        mode: "comparison" | "assertion" (default from config.DEFAULT_EVAL_CONFIG,
            i.e. "assertion")
        backend: optional compatibility label; transport is OpenAI-compatible
        model: str (default shared_vlm_judge.DEFAULT_MODEL)
        api_key: str | None
        base_url: str | None
    """
    vlm_config = eval_config.get("vlm_judge", {})

    if not vlm_config.get("enabled", False):
        return PlaceholderVLMJudge()

    # Default mode comes from config.py, the authoritative default ("assertion").
    # A second, divergent default here is how every site's per-page
    # evaluation/vlm_assertions.json got ignored: a dataset's baked
    # evaluation/eval_config.json predates assertion mode and carries no "mode" key,
    # so unless the runner is explicitly passed --vlm-mode this fell through to
    # "comparison" and judged against COMPARISON_ASSERTIONS / vlm_checklist.json.
    try:
        from config import DEFAULT_EVAL_CONFIG
        default_mode = (DEFAULT_EVAL_CONFIG.get("vlm_judge") or {}).get("mode", "assertion")
    except Exception:  # pragma: no cover - config always importable in the runner
        default_mode = "assertion"

    return AssertionVLMJudge(
        mode=vlm_config.get("mode") or default_mode,
        backend=vlm_config.get("backend", "openai-compatible"),
        model=vlm_config.get("model", shared_vlm_judge.DEFAULT_MODEL),
        api_key=vlm_config.get("api_key"),
        base_url=vlm_config.get("base_url"),
        max_concurrency=vlm_config.get("max_concurrency", 8),
    )
