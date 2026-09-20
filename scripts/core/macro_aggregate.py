#!/usr/bin/env python3
"""Unified 5-platform macro metric over the per-task metrics.json.

Each platform reports a non-comparable native `task_score` (linux/mac/win = programmatic
pass-rate; android = macro-avg over a frozen manifest; web = 4-dim weighted). This computes three
cross-platform-comparable macros from the per-task programmatic + VLM scores that
the unified metrics.json contract (``core.metrics_contract.assemble_metrics``) already emits:

    macro_prog          = mean(program_score)                over the scored set
    macro_vlm           = mean(vlm_score)                    over the scored set
    macro_prog_vlm_avg  = mean((program_score + vlm_score)/2) over the scored set
                        ( == (macro_prog + macro_vlm) / 2 , since the denominator is shared )

Scored set = every task except explicitly never-scored work
(``excluded_from_scoring`` or ``task_score is None``). Refusals are completed model outcomes
and count as 0. A dimension that did not run counts as 0 (the chosen 口径).

Integrity: this NEVER recomputes a per-task score. It only macro-averages the already-computed
``program_score``/``vlm_score``. Robust to the current top-level fields and the older nested
``programmatic.pass_rate`` / ``vlm.pass_rate`` shape.

    python -m core.macro_aggregate --metrics-dir <dir> [--platform linux] [--out macro.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # scripts/


def _f(v) -> float:
    """Coalesce to float; None / non-numeric -> 0.0."""
    try:
        return float(v) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def _pick(m: dict, top_key: str, nested: tuple[str, str]) -> float:
    """Score coalescer: top-level key -> nested {programmatic|vlm}.pass_rate -> the same under a
    'metrics' block -> 0.0. Handles the unified top-level shape and older nested
    nested shapes uniformly."""
    if not isinstance(m, dict):
        return 0.0
    if m.get(top_key) is not None:
        return _f(m.get(top_key))
    sub = m.get(nested[0])
    if isinstance(sub, dict) and sub.get(nested[1]) is not None:
        return _f(sub.get(nested[1]))
    inner = m.get("metrics")
    if isinstance(inner, dict):
        return _pick(inner, top_key, nested)
    return 0.0


def program_score(m: dict) -> float:
    return _pick(m, "program_score", ("programmatic", "pass_rate"))


def vlm_score(m: dict) -> float:
    return _pick(m, "vlm_score", ("vlm", "pass_rate"))


def is_excluded(m: dict) -> bool:
    """Drop only explicitly never-scored tasks from the denominator.

    A refusal is a completed model outcome and must arrive as a real zero. A task that entered
    eval and scored 0 is likewise not excluded.
    """

    def _flag(d) -> bool:
        return isinstance(d, dict) and d.get("excluded_from_scoring") is True

    if not isinstance(m, dict):
        return True
    if "task_score" in m and m.get("task_score") is None:
        return True
    return _flag(m) or _flag(m.get("metrics"))


def _mean(xs: list[float]) -> float | None:
    return round(sum(xs) / len(xs), 4) if xs else None


def macro_aggregate(metrics: list[dict], platform: str | None = None) -> dict:
    """Per-task unified metrics.json list -> the 3 macros over the scored set."""
    included = [m for m in metrics if not is_excluded(m)]
    progs = [program_score(m) for m in included]
    vlms = [vlm_score(m) for m in included]
    avgs = [(p + v) / 2 for p, v in zip(progs, vlms)]
    return {
        "platform": platform,
        "n_total": len(metrics),
        "n_scored": len(included),
        "n_excluded": len(metrics) - len(included),
        "macro_prog": _mean(progs),
        "macro_vlm": _mean(vlms),
        "macro_prog_vlm_avg": _mean(avgs),
    }


def load_metrics_dir(path: str) -> list[dict]:
    """Every metrics.json under a directory (one per task) — the canonical input source."""
    out: list[dict] = []
    for f in sorted(Path(path).rglob("metrics.json")):
        try:
            out.append(json.loads(f.read_text()))
        except (json.JSONDecodeError, OSError):
            pass
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="unified 5-platform macro metric (macro_prog/macro_vlm/macro_prog_vlm_avg)"
    )
    p.add_argument(
        "--metrics-dir",
        help="directory of per-task metrics.json (canonical)",
        required=True,
    )
    p.add_argument("--platform", default=None)
    p.add_argument("--out", default=None, help="write the macro report JSON here")
    return p


def main() -> int:
    a = build_parser().parse_args()
    metrics = load_metrics_dir(a.metrics_dir)
    report = macro_aggregate(metrics, platform=a.platform)
    print(json.dumps(report, indent=2))
    print(
        f"[macro] platform={report['platform']} n_scored={report['n_scored']} "
        f"n_excluded={report['n_excluded']} macro_prog={report['macro_prog']} "
        f"macro_vlm={report['macro_vlm']} macro_prog_vlm_avg={report['macro_prog_vlm_avg']}"
    )
    if a.out:
        out = Path(a.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
