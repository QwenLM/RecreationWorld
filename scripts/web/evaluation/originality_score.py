"""Originality / structural-overlap monitor for RecreationBench Web.

This is a fingerprint-independent audit signal for the case where an agent scrapes the
original site and "launders" the build fingerprints (renames the
auto-generated hash classes the ``scrape_gate`` keys on) — or, as in the atom.io evasion,
injects the original HTML verbatim on a fingerprint-less static target.

We compare the agent's captured runtime DOM (``agent_dom/<page>/dom_snapshot.json``) to GT's
(``gt_dom/<page>/dom_snapshot.json``) on the **same page**, via k=12 shingle containment of
GT-in-agent. Two signatures are computed per page:

  * tag-only skeleton (``max_containment``)  — MONITORING ONLY. A faithful honest rebuild of a
    simple/low-entropy page *also* reaches very high tag-only containment (measured on honest
    opus rebuilds: jquery.com 0.97, webpack.js.org 0.91, up to ~0.996 on code/doc pages), so
    tag-only similarity **cannot** separate honest rebuild from scrape and is unsafe to cap on.

  * tag+class skeleton (``max_containment_classed``) — the high-overlap signal. The shingle token is
    ``tag|class1,class2,…``. A verbatim copy reproduces GT's tags **and** its class names, so it
    stays high; an honest rebuild authors its OWN class names (Tailwind / semantic), so even when
    it converges on the tag skeleton the classes differ and containment collapses. Empirically
    validated on the glm-5.2 070315 run (agent_dom vs dataset gt_dom):

        scrape   atom.io   tag-only 0.9502 -> tag+class 0.8399
        scrape   basecamp  tag-only 0.8523 -> tag+class 0.8205
        honest   aew.com   tag-only 0.6076 -> tag+class 0.4838   (reused 94% of orig class NAMES,
                                                                   yet still separates — tag
                                                                   STRUCTURE diverges)
        honest   bitwig    tag-only 0.5979 -> tag+class 0.0000
        honest   3b1b      tag-only 0.3883 -> tag+class 0.2042
        honest   others    tag+class <= 0.39

    Clean gap [0.48 honest  |  0.82 scrape]; the monitoring threshold sits at 0.65. This is the change
    that detects the atom.io class of laundered scrape (which ``scrape_gate`` signals B/C/D miss
    on a fingerprint-less static site) WITHOUT false-positiving on honest high-fidelity or
    class-reusing rebuilds — the exact failure mode that kept the earlier tag-only version
    monitoring-only.

The enforcing scrape defences are ``scrape_gate.py`` and ``leak_scan.py``. This module records
an outcome-level similarity signal for audit, but does not alter the score: a faithful recreation
is expected to converge on the reference DOM and can legitimately exceed this threshold.
"""

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

SHINGLE_K = 12               # tag-gram length
HIGH_OVERLAP_FLAG = 0.97     # separate tag-only advisory threshold
# tag+class containment at/above this is reported as high overlap. Tightened 0.65 -> 0.58
# (2026-07-07): the primary, mechanism-agnostic scrape defence is now scrape_gate signal F
# (source-level class provenance), which catches the grey rendered-DOM copies this signal used
# to miss (e.g. umich 0.573, jquery 0.612 both fire F). The 0.58 monitoring threshold preserves
# the historical honest/scrape boundary and remains useful for analyzing false positives.
CAP_THRESHOLD = 0.58
GREY_BAND_LOW = 0.45
# Retained only for backward-compatible diagnostics in existing score artifacts. The evaluator
# no longer applies this value; scrape_gate/leak_scan own enforceable integrity caps.
ORIGINALITY_CAP = 0.10
MIN_GT_TAGS = 150            # ignore tiny pages (coincidental overlap dominates)


def _skeleton(node, out, with_class=False):
    """Pre-order token list of a serialized DOM body (text nodes dropped).

    ``with_class=False`` -> tag only (monitoring). ``with_class=True`` -> ``tag|sortedclasses``
    (the cap signature: classes are sorted so a mere class-reordering cannot launder a copy)."""
    if not node or not isinstance(node, dict) or node.get("type") == "text":
        return
    tag = node.get("tag")
    if tag:
        if with_class:
            cls = node.get("classes") or []
            if not isinstance(cls, list):
                cls = [str(cls)]
            out.append(tag + "|" + ",".join(sorted(str(c) for c in cls)))
        else:
            out.append(tag)
    for ch in node.get("children", []):
        _skeleton(ch, out, with_class)


def _kgrams(tags, k):
    if len(tags) < k:
        return set()
    return {tuple(tags[i:i + k]) for i in range(len(tags) - k + 1)}


def _containment(gt_grams, ag_grams):
    if not gt_grams:
        return None
    return len(gt_grams & ag_grams) / len(gt_grams)


def compute_originality(gt_dom_dir, agent_dom_dir, target_pages,
                        k: int = SHINGLE_K,
                        flag_threshold: float = HIGH_OVERLAP_FLAG,
                        cap_threshold: float = CAP_THRESHOLD,
                        grey_low: float = GREY_BAND_LOW,
                        min_gt_tags: int = MIN_GT_TAGS) -> dict:
    """Measure structural overlap without changing the submission score.

    ``penalize`` and ``cap`` remain in the result for artifact compatibility, but ``penalize``
    is always false. ``threshold_exceeded`` is the explicit monitoring signal.
    """
    from evaluation.structural_score import _load_body  # robust body loader

    gt_dom_dir = Path(gt_dom_dir)
    agent_dom_dir = Path(agent_dom_dir)

    pages = {}
    max_cont = None            # tag-only (monitoring)
    max_cont_classed = None    # tag+class (cap driver)
    max_classed_page = None

    for pid in target_pages:
        # pid is normally a page-id string (gt & agent share it). It may also be a
        # (gt_id, agent_id) pair — used only by the clean-room accept gate when the synth side
        # was URL-slug de-branded so its ids differ from the read-only dataset_v2 gt_dom ids.
        gt_pid, ag_pid = (pid, pid) if isinstance(pid, str) else (pid[0], pid[1])
        gt_body = _load_body(gt_dom_dir / gt_pid / "dom_snapshot.json")
        if gt_body is None:
            pages[ag_pid] = {"containment": None, "reason": "no_gt_dom"}
            continue
        gt_tags = []
        _skeleton(gt_body, gt_tags)
        if len(gt_tags) < min_gt_tags:
            pages[ag_pid] = {"containment": None, "reason": "gt_too_small",
                             "gt_tags": len(gt_tags)}
            continue
        ag_body = _load_body(agent_dom_dir / ag_pid / "dom_snapshot.json")
        if ag_body is None:
            pages[ag_pid] = {"containment": None, "reason": "no_agent_dom"}
            continue
        ag_tags = []
        _skeleton(ag_body, ag_tags)
        # tag+class signatures (the cap driver)
        gt_tc = []
        _skeleton(gt_body, gt_tc, with_class=True)
        ag_tc = []
        _skeleton(ag_body, ag_tc, with_class=True)

        cont = _containment(_kgrams(gt_tags, k), _kgrams(ag_tags, k))
        cont_classed = _containment(_kgrams(gt_tc, k), _kgrams(ag_tc, k))
        if cont is None:
            pages[ag_pid] = {"containment": None, "reason": "no_gt_grams"}
            continue
        pages[ag_pid] = {"containment": round(cont, 4),
                         "containment_classed": (round(cont_classed, 4)
                                                 if cont_classed is not None else None),
                         "gt_tags": len(gt_tags), "agent_tags": len(ag_tags)}
        if max_cont is None or cont > max_cont:
            max_cont = cont
        if cont_classed is not None and (max_cont_classed is None or cont_classed > max_cont_classed):
            max_cont_classed = cont_classed
            max_classed_page = ag_pid

    threshold_exceeded = (
        max_cont_classed is not None and max_cont_classed >= cap_threshold
    )
    # Grey band: elevated tag+class containment below the high-overlap threshold.
    grey_flag = (max_cont_classed is not None
                 and grey_low <= max_cont_classed < cap_threshold)

    return {
        "monitoring_only": True,
        "threshold_exceeded": threshold_exceeded,
        "penalize": False,
        "grey_flag": grey_flag,
        "max_containment": (round(max_cont, 4) if max_cont is not None else None),
        "max_containment_classed": (round(max_cont_classed, 4)
                                    if max_cont_classed is not None else None),
        "max_classed_page": max_classed_page,
        "flag": (max_cont is not None and max_cont >= flag_threshold),   # tag-only advisory
        "k": k,
        "flag_threshold": flag_threshold,
        "cap_threshold": cap_threshold,
        "cap": ORIGINALITY_CAP,
        "pages": pages,
    }
