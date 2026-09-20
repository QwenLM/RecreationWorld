#!/usr/bin/env python3
"""Shared state-normalization for pipelines — reused by every platform whose
eval stage emits ``programmatic_results.json`` + ``vlm_results.json`` (linux,
windows, macOS; android/web supply their own eval dicts and just call
``normalize``).

Splits the two reusable concerns out of any single platform pipeline:
  * ``read_eval_dir`` — parse a dir's programmatic_results.json + vlm_results.json
    into the common score dict (via core.metrics_contract.eval_scores),
  * ``normalize`` — flatten heterogeneous stage records to {name: canonical} and
    assemble the normalized platform state (pure; no IO).

This is exactly the "merge what's reusable" step: linux and windows had separate
copies of read-two-json-files + flatten-stages; now there is one.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from core import exit_contract, metrics_contract

# raw stage status -> canonical (linux/windows use "pass"; macOS uses "completed")
DEFAULT_STATUS_MAP = {
    "pass": "pass",
    "passed": "pass",
    "completed": "pass",
    "success": "pass",
    "ok": "pass",
    "skipped": "not_run",
}


def read_eval_dir(eval_dir: Path) -> Optional[dict]:
    """Read programmatic_results.json (+ vlm_results.json) from a dir into the
    common score dict, or None if neither exists."""
    eval_dir = Path(eval_dir)

    def _load(name: str) -> Optional[dict]:
        p = eval_dir / name
        try:
            return json.loads(p.read_text())
        except Exception:
            return None

    prog = _load("programmatic_results.json")
    vlm = _load("vlm_results.json")
    if prog is None and vlm is None:
        return None
    return metrics_contract.eval_scores(programmatic=prog, vlm=vlm)


def canon_status(raw, status_map: Optional[dict] = None) -> str:
    smap = status_map or DEFAULT_STATUS_MAP
    if isinstance(raw, dict):
        raw = raw.get("status", "fail")
    s = str(raw).strip().lower()
    return smap.get(s, s if s in ("timeout", "error", "not_run") else "fail")


def normalize(
    task_id: str,
    model: str,
    raw_stages: dict,
    evals: Optional[dict] = None,
    *,
    status_map: Optional[dict] = None,
    summary: Optional[dict] = None,
    pipeline_exit: Optional[int] = None,
) -> dict:
    """Assemble the unified adapter-contract state.

    ``raw_stages`` maps stage-name -> status record (dict-with-status or str);
    flattened to canonical via ``status_map``. ``evals`` maps stage-name ->
    score dict (from read_eval_dir or metrics_contract.eval_scores). ``pipeline_exit``
    defaults to 0 iff all stages pass. Pure — no IO.
    """
    stages = {}
    stage_outcomes = {}
    for name, rec in (raw_stages or {}).items():
        status = canon_status(rec, status_map)
        raw = rec if isinstance(rec, dict) else {}
        native_exit = raw.get("native_exit_code", raw.get("exit_code"))
        if isinstance(native_exit, bool) or not isinstance(native_exit, int):
            native_exit = None
        stage_outcomes[name] = exit_contract.outcome_from_stage_status(
            status,
            raw=raw,
            native_exit_code=native_exit,
        )
        stages[name] = (
            "pass"
            if stage_outcomes[name]["status"] == exit_contract.STAGE_PASS
            else (
                "not_run"
                if stage_outcomes[name]["status"] == exit_contract.STAGE_NOT_RUN
                else "fail"
            )
        )
    evals = {k: v for k, v in (evals or {}).items() if v}
    declared_pipeline_exit = pipeline_exit
    if declared_pipeline_exit is None:
        declared_pipeline_exit = (
            0 if (stages and all(v == "pass" for v in stages.values())) else 1
        )
    state = {
        "task_id": task_id,
        "model": model,
        "stages": stages,
        "evals": evals,
        "native_exit_code": int(declared_pipeline_exit),
        "summary": summary,
    }
    return exit_contract.apply_stage_outcomes(state, stage_outcomes, tuple(stages))
