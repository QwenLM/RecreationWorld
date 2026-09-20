"""Visual score for RecreationBench Web.

Composition (per page, per viewport):

    page_vp = gmean(of available)          # geometric mean — pulled down by weakest
              - SSIM         : patch-wise SSIM on tall pages, aggregated at the
                               PATCH_PERCENTILE-th percentile (default p10 — strict:
                               penalises the worst vertical bands; avoids tall-page
                               global wash-out); plain RGB SSIM on short pages.
              - LPIPS_sim    : learned perceptual similarity (1 - LPIPS), replaces
                               the old layout-blind colour histogram entirely.
              - Layout-IoU   : DOM bounding-box placement similarity (height-
                               invariant; only used when layout JSONs are present)
    page_vp *= coverage      : SYMMETRIC content-completeness penalty (P2-1). Penalises
                               the agent page being EITHER shorter (missing content) OR
                               taller (padding/extra content that escapes the cropped
                               SSIM) than GT, after normalising height by image width so
                               equivalent DSF=1 and DSF=3 captures have equal coverage.

    region   = strict percentile (p25) over {header, main-content, footer} of
               gmean(SSIM, LPIPS)          # only when region screenshots are present;
               "hero" was dropped (ambiguous selector caused unfair near-zero scores).

    VS_page  = (1-REGION_BLEND)*fullpage + REGION_BLEND*region   (region blended if present)
               REGION_BLEND defaults to 0.2 — region crops are lenient/inflating, so the
               full-page comparison dominates.
    VS_site  = mean over target pages of  (desktop*0.6 + mobile*0.4)   (RESOLUTION_WEIGHTS)

Design notes:
- All image comparisons first NORMALISE the two images to a common width
  (resize the wider one down, aspect-preserved). This makes the metric robust to
  GT/agent DPR mismatch (e.g. GT 375 vs agent 1125) even before GT is regenerated.
- LPIPS is loaded lazily as a cached singleton on CPU; protobuf is forced to the
  pure-python impl before importing lpips to avoid the onnx/protobuf C-ext clash.
- Missing-data policy (P2-2 — no upward bias): missing GT → that unit is SKIPPED
  (blend None / page score None / excluded from the site mean), NOT scored 1.0.
  Missing agent artefact (GT present) → 0 for that component (the agent failed to render).
  Missing layout/region data → that component is simply omitted from the mean.
"""

import logging
import math
import os
from pathlib import Path

import numpy as np
from PIL import Image
from skimage.metrics import structural_similarity as ssim

Image.MAX_IMAGE_PIXELS = 500_000_000
logger = logging.getLogger(__name__)

# ---- tunables -------------------------------------------------------------
SSIM_WEIGHT = 0.5          # within the pixel-similarity blend (SSIM vs LPIPS)
LPIPS_WEIGHT = 0.5
PATCH_ROWS = 8             # vertical patches for patch-wise SSIM on tall pages
PATCH_PERCENTILE = 10      # strict aggregation: 10th percentile of patch SSIMs (aggressive — worst regions)
TALL_RATIO = 2.0           # height/width beyond which we switch to patch-wise SSIM
NORM_MAX_DIM = 1600        # cap longest side after width-normalisation (speed/OOM)
REGION_BLEND = 0.2         # weight of region score (aggressive: region crops are lenient/inflating)
LAYOUT_KEY_TAGS = {"header", "nav", "main", "section", "footer",
                   "h1", "h2", "h3", "img", "article", "aside"}
LAYOUT_ROLE_MAP = {
    "header": "structural", "nav": "structural", "main": "structural",
    "section": "structural", "footer": "structural", "article": "structural",
    "aside": "structural", "h1": "heading", "h2": "heading", "h3": "heading",
    "img": "media",
}
MIN_ELEMENT_AREA_PCT = 0.003
MAX_ELEMENT_AREA_PCT = 0.50


def _gmean(xs) -> float:
    """Geometric mean over non-None values — STRICT aggregation: the result is
    pulled down by the weakest component (a page that's structurally close but
    perceptually off scores low), yet equals 1.0 only when every component is 1.0
    (so a perfect/identity replica is preserved)."""
    vals = [max(1e-6, float(x)) for x in xs if x is not None]
    if not vals:
        return 0.0
    return float(math.exp(sum(math.log(v) for v in vals) / len(vals)))

# ---------------------------------------------------------------------------
# LPIPS (lazy singleton, CPU)
# ---------------------------------------------------------------------------
_LPIPS_FN = None
_LPIPS_TRIED = False


def _get_lpips():
    """Load (once) and return the LPIPS model, or None if unavailable."""
    global _LPIPS_FN, _LPIPS_TRIED
    if _LPIPS_TRIED:
        return _LPIPS_FN
    _LPIPS_TRIED = True
    # Force pure-python protobuf BEFORE importing lpips (avoids onnx/protobuf
    # C-extension TypeError: "Descriptors cannot be created directly").
    os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")
    try:
        import torch  # noqa: F401
        import lpips
        fn = lpips.LPIPS(net="alex", verbose=False)
        fn.eval()
        _LPIPS_FN = fn
        logger.info("LPIPS(alex) loaded for visual scoring")
    except Exception as e:
        _LPIPS_FN = None
        logger.warning("LPIPS unavailable (%s) — perceptual term omitted", e)
    return _LPIPS_FN


# ---------------------------------------------------------------------------
# image loading + normalisation
# ---------------------------------------------------------------------------
def _load(path) -> Image.Image | None:
    try:
        return Image.open(path).convert("RGB")
    except Exception as e:
        logger.warning("image load failed %s: %s", path, e)
        return None


def _normalize_pair(gt: Image.Image, ag: Image.Image):
    """Resize both to a common width (wider→narrower, aspect-preserved), cap the
    longest side, and top-align crop to the shared height. Returns (gt_arr, ag_arr)
    numpy RGB. This neutralises DPR/width mismatch between GT and agent."""
    W = min(gt.size[0], ag.size[0])
    if W < 2:
        return None, None

    def rw(im):
        s = W / im.size[0]
        return im.resize((W, max(1, int(round(im.size[1] * s)))), Image.LANCZOS)

    gt, ag = rw(gt), rw(ag)
    # cap longest side for speed/memory (apply same scale to both for alignment)
    longest = max(gt.size[1], ag.size[1], W)
    if longest > NORM_MAX_DIM:
        s = NORM_MAX_DIM / longest
        nw = max(2, int(round(W * s)))
        gt = gt.resize((nw, max(1, int(round(gt.size[1] * s)))), Image.LANCZOS)
        ag = ag.resize((nw, max(1, int(round(ag.size[1] * s)))), Image.LANCZOS)
    h = min(gt.size[1], ag.size[1])
    gt = gt.crop((0, 0, gt.size[0], h))
    ag = ag.crop((0, 0, ag.size[0], h))
    return np.array(gt), np.array(ag)


def _rgb_ssim(a: np.ndarray, b: np.ndarray) -> float:
    mind = min(a.shape[0], a.shape[1])
    win = min(7, mind)
    if win % 2 == 0:
        win -= 1
    if win < 3:
        return 0.0
    vals = []
    for c in range(3):
        s = ssim(a[:, :, c], b[:, :, c], win_size=win, gaussian_weights=True)
        vals.append(max(0.0, s))
    return float(np.mean(vals))


def _patch_ssim(a: np.ndarray, b: np.ndarray, rows: int = PATCH_ROWS,
                percentile: int = PATCH_PERCENTILE) -> float:
    """Strict SSIM for tall pages: split into `rows` vertical bands, SSIM each,
    return the `percentile`-th value (lower percentile = stricter)."""
    H = a.shape[0]
    if H < rows * 8:  # too short to band meaningfully
        return _rgb_ssim(a, b)
    vals = []
    for i in range(rows):
        y0, y1 = int(i * H / rows), int((i + 1) * H / rows)
        pa, pb = a[y0:y1], b[y0:y1]
        if min(pa.shape[0], pa.shape[1]) < 3:
            continue
        vals.append(_rgb_ssim(pa, pb))
    if not vals:
        return _rgb_ssim(a, b)
    return float(np.percentile(vals, percentile))


def _lpips_patched(gt: Image.Image, ag: Image.Image, max_patches: int = 10) -> float | None:
    """Learned perceptual similarity (1 - mean LPIPS) computed on SQUARE patches so
    it works on tall pages too (mobile full-pages, where an aspect-preserving global
    resize would squish one dimension below AlexNet's minimum). Resizes both to a
    common width (<=512, aspect-preserved), top-crops to the shared height, splits
    into W×W vertical patches (sampled to <=max_patches to bound CPU cost), LPIPS
    each, and averages. Returns None if LPIPS is unavailable or the content is too
    thin/small (e.g. a wide-short region crop) — caller then uses SSIM only."""
    fn = _get_lpips()
    if fn is None:
        return None
    try:
        import torch
        W = min(gt.size[0], ag.size[0])
        if W < 64:
            return None
        W = min(W, 512)

        def rw(im):
            s = W / im.size[0]
            return im.resize((W, max(1, int(round(im.size[1] * s)))), Image.LANCZOS)

        g = np.array(rw(gt))
        a = np.array(rw(ag))
        H = min(g.shape[0], a.shape[0])
        if H < 64:
            return None
        g, a = g[:H, :W], a[:H, :W]
        ph = W if H > W else H
        ys = list(range(0, H - ph + 1, ph)) or [0]
        if ys and ys[-1] + ph < H:
            ys.append(H - ph)
        if len(ys) > max_patches:  # sample evenly to bound CPU cost
            idx = sorted(set(round(i * (len(ys) - 1) / (max_patches - 1)) for i in range(max_patches)))
            ys = [ys[i] for i in idx]

        def t(x):
            return torch.from_numpy(x.astype(np.float32) / 127.5 - 1.0).permute(2, 0, 1).unsqueeze(0)

        dists = []
        with torch.no_grad():
            for y in ys:
                gp, ap = g[y:y + ph], a[y:y + ph]
                if min(gp.shape[0], gp.shape[1]) < 64:
                    continue
                dists.append(fn(t(gp), t(ap)).item())
        if not dists:
            return None
        return max(0.0, 1.0 - float(np.mean(dists)))
    except Exception as e:
        logger.debug("LPIPS skipped (%s)", str(e)[:80])
        return None


# ---------------------------------------------------------------------------
# pixel-similarity of one screenshot pair (SSIM_p25 + LPIPS)
# ---------------------------------------------------------------------------
def compute_pixel_similarity(gt_path: str, agent_path: str) -> dict:
    """Return similarity plus the original width/height of both images.

    blend = SSIM_WEIGHT*ssim + LPIPS_WEIGHT*lpips, renormalised if lpips missing.
    Missing GT → blend None (caller SKIPS this unit — no upward 1.0 bias, P2-2);
    missing agent (GT present) → blend 0.0 (agent failed to render).
    """
    gp, ap = Path(gt_path), Path(agent_path)
    if not gp.exists():
        return {"ssim": None, "lpips": None, "blend": None,
                "gt_w": 0, "gt_h": 0, "agent_w": 0, "agent_h": 0,
                "note": "no_gt"}
    if not ap.exists():
        return {"ssim": 0.0, "lpips": 0.0, "blend": 0.0,
                "gt_w": 0, "gt_h": 0, "agent_w": 0, "agent_h": 0,
                "note": "no_agent"}
    gt, ag = _load(gt_path), _load(agent_path)
    if gt is None or ag is None:
        return {"ssim": 0.0, "lpips": None, "blend": 0.0,
                "gt_w": 0, "gt_h": 0, "agent_w": 0, "agent_h": 0,
                "note": "load_fail"}
    gt_w, gt_h = gt.size
    agent_w, agent_h = ag.size
    a, b = _normalize_pair(gt, ag)
    if a is None:
        return {"ssim": 0.0, "lpips": None, "blend": 0.0,
                "gt_w": gt_w, "gt_h": gt_h,
                "agent_w": agent_w, "agent_h": agent_h,
                "note": "norm_fail"}
    tall = (a.shape[0] / max(1, a.shape[1])) > TALL_RATIO
    ssim_val = _patch_ssim(a, b) if tall else _rgb_ssim(a, b)
    lpips_val = _lpips_patched(gt, ag)
    if lpips_val is None:
        blend = ssim_val
    else:
        blend = _gmean([ssim_val, lpips_val])  # geometric mean: strict — penalises the weaker metric
    return {"ssim": round(ssim_val, 4),
            "lpips": round(lpips_val, 4) if lpips_val is not None else None,
            "blend": round(blend, 4),
            "gt_w": gt_w, "gt_h": gt_h,
            "agent_w": agent_w, "agent_h": agent_h}


def coverage_ratio(gt_w: int, gt_h: int, agent_w: int, agent_h: int) -> float:
    """SYMMETRIC content-completeness penalty (P2-1 / bug #5 ①).

    The pixel comparison crops both images to the shared (shorter) height, so content
    beyond that height escapes SSIM entirely. The old version only penalised the agent
    being SHORTER than GT (missing content), giving a free pass to padding the page TALLER
    to push real content below the cropped comparison region. We now penalise BOTH
    directions by the ratio of width-normalised heights. Raw pixel height is invalid here:
    the same page captured at DSF=1 and DSF=3 differs by 3x in both dimensions but has identical
    content coverage.

        coverage = min(gt_h / gt_w, agent_h / agent_w)
                   / max(gt_h / gt_w, agent_h / agent_w)

    """
    if gt_w <= 0 or gt_h <= 0 or agent_w <= 0 or agent_h <= 0:
        return 1.0
    gt_ratio = gt_h / gt_w
    agent_ratio = agent_h / agent_w
    return min(gt_ratio, agent_ratio) / max(gt_ratio, agent_ratio)

# ---------------------------------------------------------------------------
# Layout-IoU (DOM bounding-box placement) — adapted from v1
# ---------------------------------------------------------------------------
def _bbox_iou(a: dict, b: dict) -> float:
    x1, y1 = max(a["x"], b["x"]), max(a["y"], b["y"])
    x2 = min(a["x"] + a["w"], b["x"] + b["w"])
    y2 = min(a["y"] + a["h"], b["y"] + b["h"])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    union = a["w"] * a["h"] + b["w"] * b["h"] - inter
    return inter / union if union > 0 else 0.0


def _optimal_match(iou_matrix):
    n_gt = len(iou_matrix)
    if n_gt == 0:
        return []
    try:
        from scipy.optimize import linear_sum_assignment
        cost = np.array(iou_matrix)
        r_ind, c_ind = linear_sum_assignment(-cost)
        res = [0.0] * n_gt
        for r, c in zip(r_ind, c_ind):
            res[r] = float(iou_matrix[r][c])
        return res
    except Exception:
        used, res = set(), []
        ncols = len(iou_matrix[0]) if iou_matrix else 0
        for i in range(n_gt):
            best, bj = 0.0, -1
            for j in range(ncols):
                if j not in used and iou_matrix[i][j] > best:
                    best, bj = iou_matrix[i][j], j
            if bj >= 0:
                used.add(bj)
            res.append(best)
        return res


def compute_layout_iou(gt_layout: dict, agent_layout: dict) -> float:
    """Role-grouped, normalised-coordinate, Hungarian-matched bbox IoU.
    layout dict format: {'viewport':{'width':...}, 'page_height':..,
                         'elements':[{'tag':, 'bbox':{'x','y','w','h'}}, ...]}"""
    from collections import defaultdict
    gt_el = [e for e in gt_layout.get("elements", []) if e.get("tag") in LAYOUT_KEY_TAGS]
    ag_el = [e for e in agent_layout.get("elements", []) if e.get("tag") in LAYOUT_KEY_TAGS]
    if not gt_el:
        return 1.0 if not ag_el else 0.0
    gw = gt_layout.get("viewport", {}).get("width", 1920) or 1920
    gh = gt_layout.get("page_height", 1) or 1
    aw = agent_layout.get("viewport", {}).get("width", 1920) or 1920
    ah = agent_layout.get("page_height", 1) or 1
    gt_total, ag_total = gw * gh, aw * ah

    def norm(b, pw, ph):
        return {"x": b["x"] / pw, "y": b["y"] / ph, "w": b["w"] / pw, "h": b["h"] / ph}

    def apct(b, total):
        return (b["w"] * b["h"] / total) if total > 0 else 0

    gt_by, ag_by = defaultdict(list), defaultdict(list)
    for e in gt_el:
        if MIN_ELEMENT_AREA_PCT < apct(e["bbox"], gt_total) < MAX_ELEMENT_AREA_PCT:
            gt_by[LAYOUT_ROLE_MAP.get(e["tag"], "other")].append(norm(e["bbox"], gw, gh))
    for e in ag_el:
        if MIN_ELEMENT_AREA_PCT < apct(e["bbox"], ag_total) < MAX_ELEMENT_AREA_PCT:
            ag_by[LAYOUT_ROLE_MAP.get(e["tag"], "other")].append(norm(e["bbox"], aw, ah))

    ious = []
    for role, gels in gt_by.items():
        aels = ag_by.get(role, [])
        if not aels:
            ious.extend([0.0] * len(gels))
            continue
        m = [[_bbox_iou(g, a) for a in aels] for g in gels]
        ious.extend(_optimal_match(m))
    return float(np.mean(ious)) if ious else 0.0


# ---------------------------------------------------------------------------
# page / site scoring
# ---------------------------------------------------------------------------
def compute_visual_score_for_page(
    page_id: str,
    gt_screenshots_dir: Path,
    agent_screenshots_dir: Path,
    gt_layout_dir: Path = None,
    agent_layout_dir: Path = None,
    gt_region_dir: Path = None,
    agent_region_dir: Path = None,
) -> dict:
    """Visual score for one page: fullpage (SSIM+LPIPS+layout) × symmetric-coverage,
    blended with region score when region screenshots exist."""
    from config import RESOLUTION_WEIGHTS
    import json

    per_vp = {}
    for vp_name, weight in RESOLUTION_WEIGHTS.items():
        gt_p = gt_screenshots_dir / page_id / f"{vp_name}.png"
        ag_p = agent_screenshots_dir / page_id / f"{vp_name}.png"
        if not gt_p.exists():
            continue  # no GT for this viewport → skip (no penalty, P2-2)
        sim = compute_pixel_similarity(str(gt_p), str(ag_p))
        if sim["blend"] is None:
            continue  # no GT (defensive) → skip, do not award
        cov = coverage_ratio(
            sim["gt_w"], sim["gt_h"], sim["agent_w"], sim["agent_h"],
        )
        components = [sim["blend"]]

        # Layout-IoU (height-invariant) — only when both layout JSONs present
        layout_iou = None
        if gt_layout_dir and agent_layout_dir:
            gl = gt_layout_dir / page_id / f"{vp_name}.json"
            al = agent_layout_dir / page_id / f"{vp_name}.json"
            if gl.exists() and al.exists():
                try:
                    layout_iou = compute_layout_iou(json.loads(gl.read_text()),
                                                    json.loads(al.read_text()))
                    components.append(layout_iou)
                except Exception as e:
                    logger.warning("layout_iou failed %s/%s: %s", page_id, vp_name, e)

        fullpage = _gmean(components) * cov
        per_vp[vp_name] = {
            "ssim": sim["ssim"], "lpips": sim["lpips"], "pixel_blend": sim["blend"],
            "layout_iou": (round(layout_iou, 4) if layout_iou is not None else None),
            "coverage": round(cov, 4), "fullpage": round(fullpage, 4),
            "weight": weight,
            "gt_w": sim["gt_w"], "gt_h": sim["gt_h"],
            "agent_w": sim["agent_w"], "agent_h": sim["agent_h"],
        }

    if not per_vp:
        # No GT screenshots for this page at all → unscorable, SKIP (P2-2: do not
        # award 1.0). The site aggregator excludes pages with no per_viewport.
        return {"score": None, "per_viewport": {}, "note": "no_gt_screenshots"}

    tw = sum(v["weight"] for v in per_vp.values())
    fullpage_score = sum(v["fullpage"] * v["weight"] for v in per_vp.values()) / tw if tw else 0.0

    # Region score (only if region screenshots present for this page)
    region_score, region_detail = None, {}
    if gt_region_dir and agent_region_dir:
        rs = _region_score_for_page(page_id, gt_region_dir, agent_region_dir)
        if rs is not None:
            region_score, region_detail = rs

    if region_score is not None:
        score = (1 - REGION_BLEND) * fullpage_score + REGION_BLEND * region_score
    else:
        score = fullpage_score

    return {"score": round(score, 4), "fullpage": round(fullpage_score, 4),
            "region": (round(region_score, 4) if region_score is not None else None),
            "per_viewport": per_vp, "region_detail": region_detail}


def _region_score_for_page(page_id, gt_region_dir, agent_region_dir):
    """Strict-percentile (p25) over regions of gmean(SSIM, LPIPS). Regions live as
    <region_dir>/<page_id>/<region>.png. Only canonical regions in config.REGION_SELECTORS
    are scored (so legacy GT crops for dropped regions like "hero" are ignored — P2-1).
    Returns (score, detail) or None."""
    from config import REGION_SELECTORS
    gt_pdir = Path(gt_region_dir) / page_id
    if not gt_pdir.is_dir():
        return None
    scores, detail = [], {}
    for gf in sorted(gt_pdir.glob("*.png")):
        if gf.stem not in REGION_SELECTORS:
            continue  # skip regions no longer part of the canonical set
        af = Path(agent_region_dir) / page_id / gf.name
        sim = compute_pixel_similarity(str(gf), str(af))
        if sim["blend"] is None:
            continue  # missing GT region (defensive) → skip
        scores.append(sim["blend"])  # blend = gmean(SSIM, LPIPS) — strict per region
        detail[gf.stem] = sim["blend"]
    if not scores:
        return None
    # strict across-region aggregation: 25th percentile (penalise the worst region)
    return float(np.percentile(scores, 25)), detail


def compute_visual_score_for_site(
    gt_screenshots_dir: Path,
    agent_screenshots_dir: Path,
    target_pages: list,
    gt_layout_dir: Path = None,
    agent_layout_dir: Path = None,
    gt_region_dir: Path = None,
    agent_region_dir: Path = None,
) -> dict:
    """Site visual score = mean over target pages of per-page score.
    Backward compatible: extra dirs are optional and used only when present."""
    gt_screenshots_dir = Path(gt_screenshots_dir)
    agent_screenshots_dir = Path(agent_screenshots_dir)
    page_scores = {}
    for pid in target_pages:
        page_scores[pid] = compute_visual_score_for_page(
            pid, gt_screenshots_dir, agent_screenshots_dir,
            gt_layout_dir, agent_layout_dir, gt_region_dir, agent_region_dir,
        )
    valid = [s["score"] for s in page_scores.values()
             if s.get("per_viewport") and s.get("score") is not None]
    overall = float(np.mean(valid)) if valid else 0.0
    return {"overall_score": round(overall, 4), "per_page": page_scores}


# ---------------------------------------------------------------------------
# back-compat shims (older callers)
# ---------------------------------------------------------------------------
def compute_ssim(gt_path: str, agent_path: str) -> float:
    """Back-compat: width-normalised RGB SSIM of a pair (no coverage/percentile)."""
    gt, ag = _load(gt_path), _load(agent_path)
    if gt is None or ag is None:
        return 0.0
    a, b = _normalize_pair(gt, ag)
    if a is None:
        return 0.0
    return _rgb_ssim(a, b)


def compare_screenshots(agent_screenshot: str, gt_screenshot: str) -> float:
    if not Path(gt_screenshot).exists():
        return 1.0
    if not Path(agent_screenshot).exists():
        return 0.0
    return compute_ssim(gt_screenshot, agent_screenshot)
