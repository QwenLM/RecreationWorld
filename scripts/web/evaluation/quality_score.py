"""Quality Score (QS) — continuous, reference-relative quality for RecreationBench Web.

Replaces the saturated binary quality tests (console/alt/lang/selectable, which the
audit showed are ~100% for both models) with continuous signals:

  quality_scorer = A11Y_W * a11y + HYGIENE_W * code_hygiene + CONTRAST_W * contrast

- a11y  : GT-RELATIVE where GT data exists — the agent is only asked to reproduce
          a11y/semantic features the GT page actually had, but each sub-check is now
          SYMMETRIC / order-aware so the score DISCRIMINATES instead of saturating (R6):
          landmark F1 (penalises missing AND spurious landmarks), heading-outline sequence
          similarity (difflib, order-aware), single-h1 match, semantic-tag distribution
          parity (div-soup penalty), and alt-coverage — read from the captured
          dom_snapshot.json (body subtree); plus absolute live checks (html lang, viewport
          meta, descriptive link text, form-control labels) that the GT snapshot cannot
          express. The GT-relative part is PRIMARY (A11Y_GT_W). The old recall-based checks
          saturated (any template with header/nav/main/footer scored ~1.0), which made the
          dimension a near-constant that even anti-correlated with fidelity.
- code_hygiene : tuned for vite-plugin-singlefile + Tailwind output. A
          duplicate-line metric is meaningless on a one-line bundle and Tailwind
          makes css-var/inline-style non-discriminative, so we lean on the
          rendered-DOM semantic-tag ratio (div-soup penalty) + a retuned inline-
          style-density signal from source.
- contrast : WCAG AA pass fraction over sampled text nodes
          (getComputedStyle, font-size aware).

The quality dimension blends this scorer with the (tightened) binary quality test
pass-rate — see evaluation.evaluator (QUALITY_SCORER_BLEND).
"""
import difflib
import logging
import re
from collections import Counter
from pathlib import Path

from core import runtime_assets

logger = logging.getLogger(__name__)

# --- component weights inside the quality scorer (tunable) -------------------
A11Y_W = 0.55
HYGIENE_W = 0.25
CONTRAST_W = 0.20

# Within the a11y component: weight the GT-RELATIVE snapshot checks (landmark/heading/
# single-h1/alt — genuinely discriminative) above the absolute live checks (lang/viewport/
# link/form — near-saturated for any template-based output). Previously the GT-relative
# score was averaged 1:N with the live checks, so it was only ~11% of the quality score
# and the dimension saturated/anti-correlated (P2-3 / bug #6). Making it primary restores
# discrimination using only data available at eval time (no GT live page is captured).
A11Y_GT_W = 0.6

# --- code-hygiene sub-weights + thresholds for the bundled stack --------------
HYGIENE_SEMANTIC_W = 0.6      # rendered-DOM semantic ratio (primary, survives bundling)
HYGIENE_INLINE_W = 0.4        # inline-style density from source (retuned)
SEMANTIC_RATIO_TARGET = 0.15  # semantic/(semantic+div) ratio that scores 1.0
INLINE_DENSITY_ZERO = 0.03    # inline-style character density that scores 0.0

_SEMANTIC_BLOCK = {'header', 'nav', 'main', 'section', 'article', 'aside', 'footer',
                   'figure', 'figcaption', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6'}
_LANDMARKS = {'header', 'nav', 'main', 'section', 'article', 'aside', 'footer'}


# ---------------------------------------------------------------------------
# DOM-snapshot helpers (operate on the captured body subtree)
# ---------------------------------------------------------------------------

def _walk(node, fn):
    if not node or not isinstance(node, dict) or node.get("type") == "text":
        return
    fn(node)
    for ch in node.get("children", []):
        _walk(ch, fn)


def _collect(body):
    """Walk a body subtree once, collecting a11y-relevant features."""
    tags = Counter()
    headings = []
    img_total = 0
    img_with_alt = 0

    def visit(n):
        nonlocal img_total, img_with_alt
        tag = n.get("tag", "")
        if tag:
            tags[tag] += 1
        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            headings.append(tag)
        if tag == "img":
            img_total += 1
            attrs = n.get("attributes", {}) or {}
            # normalize_dom/SERIALIZE keep alt; presence (even "") counts as labeled
            if "alt" in attrs:
                img_with_alt += 1

    _walk(body, visit)
    return {
        "tags": tags,
        "headings": headings,
        "landmarks": set(tags) & _LANDMARKS,
        "img_total": img_total,
        "img_alt_cov": (img_with_alt / img_total) if img_total else None,
    }


def _multiset_overlap(expected, actual):
    expected = list(expected)
    actual = list(actual)
    if not expected:
        return 1.0
    used = [False] * len(actual)
    matched = 0
    for item in expected:
        for idx, cand in enumerate(actual):
            if not used[idx] and cand == item:
                used[idx] = True
                matched += 1
                break
    return matched / len(expected)


# --- discriminative GT-relative sub-scores (P2-3 / R6) -----------------------------------
# The old sub-checks were recall-based (landmark_recall = |GT∩A|/|GT|, heading multiset
# overlap) and so SATURATED — any React template that includes header/nav/main/footer and
# reproduces the headings scored ~1.0, making the quality dimension a near-constant that even
# anti-correlated with fidelity. These replacements penalise DIVERGENCE symmetrically so the
# score tracks how closely the agent's semantic structure matches GT's.

def _f1(gt_set, ag_set):
    """Symmetric set agreement (penalises BOTH missing and spurious members), vs the old
    one-sided recall which a superset trivially maxed out."""
    if not gt_set and not ag_set:
        return 1.0
    if not gt_set or not ag_set:
        return 0.0
    inter = len(gt_set & ag_set)
    if inter == 0:
        return 0.0
    prec, rec = inter / len(ag_set), inter / len(gt_set)
    return 2 * prec * rec / (prec + rec)


def _heading_outline_sim(gt_headings, ag_headings):
    """Order-aware similarity of the heading-LEVEL sequence (e.g. ['h1','h2','h2','h3']).
    difflib ratio ∈ [0,1]; rewards reproducing the actual document outline, not just the
    multiset of levels."""
    if not gt_headings:
        return None
    return difflib.SequenceMatcher(None, gt_headings, ag_headings).ratio()


def _semantic_profile_sim(gt_tags, ag_tags):
    """1 - ½·L1 between the NORMALISED semantic-tag frequency distributions of GT and agent.
    Discriminates whether the agent used semantic elements (section/article/nav/figure/h*/…)
    in similar PROPORTIONS to GT — a div-soup rebuild diverges here even if it happens to
    include one of each landmark. Returns None if GT has no semantic elements."""
    keys = _SEMANTIC_BLOCK
    gsum = sum(gt_tags.get(k, 0) for k in keys)
    if gsum == 0:
        return None
    asum = sum(ag_tags.get(k, 0) for k in keys)
    if asum == 0:
        return 0.0
    l1 = sum(abs(gt_tags.get(k, 0) / gsum - ag_tags.get(k, 0) / asum) for k in keys)
    return 1.0 - 0.5 * l1


def _combine_a11y(gt_rel, live_checks):
    """Combine the GT-relative a11y snapshot score (PRIMARY, discriminative) with the
    absolute live checks (saturated) — P2-3. Returns None when neither is available.

    gt_rel: float|None  GT-relative a11y sub-score (landmark/heading/single-h1/alt).
    live_checks: list[float]  absolute live signals (lang/viewport/link/form).
    """
    live_mean = (sum(live_checks) / len(live_checks)) if live_checks else None
    if gt_rel is not None and live_mean is not None:
        return A11Y_GT_W * gt_rel + (1.0 - A11Y_GT_W) * live_mean
    if gt_rel is not None:
        return gt_rel
    return live_mean


def _a11y_gt_relative(gt_body, agent_body):
    """GT-relative a11y/semantic sub-score from snapshots. Each sub-check is only applied
    where GT actually had the feature (reproduce-GT semantics), and each is SYMMETRIC /
    order-aware so it discriminates instead of saturating (R6)."""
    parts = {}
    if gt_body is None:
        return {"score": None, "parts": parts}
    g = _collect(gt_body)
    a = _collect(agent_body) if agent_body else {"tags": Counter(), "headings": [],
                                                 "landmarks": set(), "img_total": 0, "img_alt_cov": None}

    checks = []
    # landmark F1 — penalise BOTH missing GT landmarks and spurious extra ones
    if g["landmarks"]:
        v = _f1(g["landmarks"], a["landmarks"])
        parts["landmark_f1"] = round(v, 4)
        checks.append(v)
    # heading outline — order-aware sequence similarity (only if GT had headings)
    if g["headings"]:
        v = _heading_outline_sim(g["headings"], a["headings"])
        parts["heading_outline_sim"] = round(v, 4)
        checks.append(v)
        # single-h1 match (only meaningful if GT had any heading)
        gt_single = g["headings"].count("h1") == 1
        ag_single = a["headings"].count("h1") == 1
        s = 1.0 if gt_single == ag_single else 0.0
        parts["single_h1_match"] = s
        checks.append(s)
    # semantic-tag distribution parity (div-soup penalty) — only if GT used semantic tags
    sp = _semantic_profile_sim(g["tags"], a["tags"])
    if sp is not None:
        parts["semantic_profile_sim"] = round(sp, 4)
        checks.append(sp)
    # alt-coverage match — only if GT page had images with measurable alt coverage
    if g["img_alt_cov"] is not None and g["img_alt_cov"] > 0:
        ag_cov = a["img_alt_cov"] if a["img_alt_cov"] is not None else 0.0
        v = min(1.0, ag_cov / g["img_alt_cov"])
        parts["alt_match"] = round(v, 4)
        checks.append(v)

    score = (sum(checks) / len(checks)) if checks else None
    return {"score": score, "parts": parts}


# ---------------------------------------------------------------------------
# Code hygiene (re-tuned for vite-singlefile + Tailwind)
# ---------------------------------------------------------------------------

def _code_hygiene_v2(agent_output_dir: Path, agent_body) -> dict:
    # semantic ratio from the rendered DOM (primary; survives bundling)
    sem_score = 0.5
    if agent_body is not None:
        c = _collect(agent_body)
        sem = sum(c["tags"][t] for t in _SEMANTIC_BLOCK)
        div = c["tags"].get("div", 0)
        denom = sem + div
        if denom > 0:
            ratio = sem / denom
            sem_score = min(1.0, ratio / SEMANTIC_RATIO_TARGET)

    # inline-style density from source files (retuned threshold)
    inline_score = 0.5
    content = []
    try:
        for f in list(agent_output_dir.rglob("*.html"))[:50]:
            if f.is_file():
                content.append(f.read_text(encoding="utf-8", errors="replace"))
    except Exception as e:
        logger.warning("code_hygiene file read failed: %s", e)
    if content:
        combined = "\n".join(content)
        inline = re.findall(r'style="([^"]*)"', combined) + re.findall(r"style='([^']*)'", combined)
        inline_chars = sum(len(m) for m in inline)
        density = inline_chars / len(combined) if combined else 0.0
        inline_score = 1.0 - min(1.0, density / INLINE_DENSITY_ZERO)

    score = HYGIENE_SEMANTIC_W * sem_score + HYGIENE_INLINE_W * inline_score
    return {"score": score, "semantic_ratio_score": round(sem_score, 4),
            "inline_style_score": round(inline_score, 4)}


# ---------------------------------------------------------------------------
# Live checks (contrast + lang + viewport + link-text + form-labels)
# ---------------------------------------------------------------------------

_LIVE_QUALITY_JS = runtime_assets.load_text("web/runtime_assets/live_quality.js")


async def _live_quality(page, url) -> dict:
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(1200)
        return await page.evaluate(_LIVE_QUALITY_JS)
    except Exception as e:
        logger.warning("live quality checks failed for %s: %s", url, e)
        return None


# ---------------------------------------------------------------------------
# Site-level wrapper
# ---------------------------------------------------------------------------

async def compute_quality_score_for_site(browser, agent_url, agent_output_dir,
                                         gt_dom_dir, agent_dom_dir, target_pages) -> dict:
    """Continuous, GT-relative quality score over a site's target pages.

    Args:
        browser: live Playwright browser (for the per-page live checks)
        agent_url: served agent base URL
        agent_output_dir: agent build dir (for code-hygiene source analysis)
        gt_dom_dir / agent_dom_dir: dirs containing <page>/dom_snapshot.json
        target_pages: list of page ids
    Returns {overall_score: float|None, n_scored, pages, code_hygiene}.
    """
    from evaluation.structural_score import _load_body  # reuse robust body loader

    gt_dom_dir = Path(gt_dom_dir)
    agent_dom_dir = Path(agent_dom_dir)
    agent_output_dir = Path(agent_output_dir)

    pages: dict = {}
    scored: list[float] = []

    ctx = await browser.new_context(viewport={"width": 1366, "height": 900},
                                    reduced_motion="reduce")
    pg = await ctx.new_page()
    try:
        for pid in target_pages:
            gt_body = _load_body(gt_dom_dir / pid / "dom_snapshot.json")
            agent_body = _load_body(agent_dom_dir / pid / "dom_snapshot.json")

            a11y = _a11y_gt_relative(gt_body, agent_body)
            hygiene = _code_hygiene_v2(agent_output_dir, agent_body)

            page_path = "/" if pid == "homepage" else f"/{pid}/"
            live = await _live_quality(pg, agent_url.rstrip("/") + page_path)

            # absolute live a11y signals folded into the a11y component
            live_checks = []
            contrast = None
            if live is not None:
                live_checks = [float(live.get("lang", False)), float(live.get("viewport", False)),
                               float(live.get("link_score", 1.0)), float(live.get("form_score", 1.0))]
                contrast = float(live.get("contrast", 1.0))

            # combine GT-relative a11y (snapshot, discriminative) with absolute a11y
            # (live, saturated). GT-relative is the PRIMARY signal (A11Y_GT_W) so the
            # dimension stays discriminative instead of washing out to a constant (P2-3).
            a11y_score = _combine_a11y(a11y["score"], live_checks)

            # if a page has no GT dom AND no live data, it is unscorable -> skip
            if a11y_score is None and contrast is None:
                pages[pid] = {"score": None, "reason": "unscorable"}
                continue

            # Blend over the components that are actually present, RENORMALISING their weights
            # (R6): the old code injected 0.5 for a missing a11y/contrast, which floored every
            # score toward the middle and masked real divergence. Hygiene is always available.
            comps = [(HYGIENE_W, hygiene["score"])]
            if a11y_score is not None:
                comps.append((A11Y_W, a11y_score))
            if contrast is not None:
                comps.append((CONTRAST_W, contrast))
            wsum = sum(w for w, _ in comps)
            page_score = sum(w * v for w, v in comps) / wsum if wsum else 0.0
            pages[pid] = {
                "score": round(page_score, 4),
                "a11y": (round(a11y_score, 4) if a11y_score is not None else None),
                "a11y_gt_relative": (round(a11y["score"], 4) if a11y["score"] is not None else None),
                "a11y_parts": a11y["parts"],
                "contrast": (round(contrast, 4) if contrast is not None else None),
                "hygiene": round(hygiene["score"], 4),
                "hygiene_parts": {k: v for k, v in hygiene.items() if k != "score"},
            }
            scored.append(page_score)
    finally:
        await ctx.close()

    overall = (sum(scored) / len(scored)) if scored else None
    return {
        "overall_score": (round(overall, 4) if overall is not None else None),
        "n_scored": len(scored),
        "pages": pages,
    }
