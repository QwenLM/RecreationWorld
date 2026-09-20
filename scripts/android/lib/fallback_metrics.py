#!/usr/bin/env python3
"""Write the minimal Android result used when the full metrics module is unavailable."""

from __future__ import annotations

import json
import os
from pathlib import Path


def main() -> int:
    output_dir = Path(os.environ.get("OUTPUT_DIR", "/workspace/output"))
    native = int(os.environ.get("WM_EXIT", "2") or 2)
    force = os.environ.get("WM_FORCE", "")
    if force == "pod_timeout" or native in (130, 143):
        public, outcome, reason = 143, "terminated", "pod_terminated"
    else:
        public, outcome, reason = 2, "infra_error", "metrics_writer_unavailable"

    stage_token = os.environ.get("STAGE", "recreation_eval")
    raw_names = os.environ.get("STAGE_LIST", "").replace("recreation_eval", "recreation,eval")
    names = [name for name in raw_names.split(",") if name] or [stage_token]
    try:
        raw_statuses = json.loads(os.environ.get("WM_STAGES", "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        raw_statuses = {}
    target = next(
        (
            index
            for index, name in enumerate(names)
            if raw_statuses.get(name) not in ("ok", "reused")
        ),
        len(names) - 1,
    )
    outcomes = {}
    for index, name in enumerate(names):
        if index < target:
            values = ("pass", "completed", "completed", None, True, False)
        elif index == target:
            status = (
                "timeout"
                if outcome == "terminated"
                else "fail"
                if outcome == "data_error"
                else "error"
            )
            values = (status, outcome, reason, native, False, outcome != "data_error")
        else:
            values = (
                "not_run",
                outcome,
                "upstream_stage_failed",
                None,
                False,
                outcome != "data_error",
            )
        status, item_outcome, item_reason, item_native, complete, retryable = values
        outcomes[name] = {
            "status": status,
            "outcome_class": item_outcome,
            "reason_code": item_reason,
            "native_exit_code": item_native,
            "result_complete": complete,
            "retryable": retryable,
        }

    metrics = {
        "task_id": os.environ.get("TASK_ID", ""),
        "stage": stage_token,
        "pipeline_exit_code": public,
        "process_exit_code": public,
        "outcome_class": outcome,
        "reason_code": reason,
        "result_complete": False,
        "retryable": outcome in ("infra_error", "terminated"),
        "native_exit_code": native,
        "stage_outcomes": outcomes,
        "task_score": 0.0,
        "passed": False,
        "eval_status": "pipeline_error",
        "eval_error": "rb pipeline source unavailable; set rb_pipeline_commit",
    }
    for name, item in outcomes.items():
        metrics[f"stage_{name}"] = "succeeded" if item["status"] == "pass" else "failed"
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "metrics.json").write_text(json.dumps(metrics, indent=2))
    print(json.dumps(metrics, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
