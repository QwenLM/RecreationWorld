#!/usr/bin/env python3
"""Small runtime operations used by the Web worker.

Keeping these operations in a normal module makes the worker a lifecycle script
instead of a container for embedded Python programs.  The functions are also
usable by a local/open-source runner without an deployment platform environment.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

PASS_THRESHOLD = 0.75


def materialize(instance_dir: Path, dataset_dir: Path, domain: str) -> dict:
    """Materialize one frozen Web instance and validate required inputs."""
    from rb_unify.eval_bridge import materialize_web_dataset

    report = materialize_web_dataset(str(instance_dir), str(dataset_dir), domain)
    print("[mockweb]   materialised: " + json.dumps(report, ensure_ascii=False))
    if not report.get("site") or not report.get("specs"):
        raise RuntimeError("unified instance incomplete (no site or no specs)")
    if not report.get("task_json"):
        raise RuntimeError(
            "unified instance ships no task.json -- the runner reads "
            "<domain>/task.json and aborts without it (prompt, target_pages, "
            "time limit live there)"
        )
    if not report.get("site_meta"):
        print("[mockweb]   WARN: no site_meta.json -> prompt page list and scorer page map degrade")
    if report.get("needs_gt"):
        print("[mockweb]   WARN: instance ships no ground truth -> visual/structural cannot score")
    return report


def write_native_metrics(
    output_dir: Path,
    *,
    agent_rc: int,
    eval_target: str,
    scaffold: str,
) -> dict:
    """Convert the native Web runner result into its stable metrics payload."""
    run_path = output_dir / "run.json"
    try:
        run = json.loads(run_path.read_text())
        if not isinstance(run, dict):
            raise ValueError("runner result must be an object")
    except Exception as exc:  # noqa: BLE001 - malformed output is recorded as infra
        print(f"[mockweb] ERROR: failed to parse run.json: {exc}", file=__import__("sys").stderr)
        run = {}

    status = run.get("status", "unknown")
    if agent_rc and status not in (
        "api_error",
        "agent_error",
        "agent_not_available",
        "agent_terminated",
    ):
        status = "agent_terminated" if agent_rc in (130, 143) else "agent_error"
    final_score = run.get("final_score")
    try:
        final_score = float(final_score) if final_score is not None else 0.0
    except (TypeError, ValueError):
        final_score = 0.0

    scores = run.get("scores") if isinstance(run.get("scores"), dict) else {}
    metrics = {
        "eval_target": eval_target,
        "task_score": round(final_score, 4),
        "passed": status == "success" and final_score >= PASS_THRESHOLD,
        "metrics": {
            "status": status,
            "eval_target": eval_target,
            "final_score": round(final_score, 4),
            "domain": run.get("domain"),
            "task_id": run.get("task_id"),
            "difficulty": run.get("difficulty"),
            "model": run.get("model"),
            "scaffold": scaffold,
            "scores": scores,
            "anti_cheat": scores.get("anti_cheat_penalty"),
            "submitted": run.get("submitted"),
            "no_submission_reason": run.get("no_submission_reason"),
            "rollout": run.get("rollout", {}),
            "api_error": run.get("api_error"),
            "build_result": run.get("build_result", {}),
            "eval_error": run.get("eval_error"),
        },
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "metrics.json").write_text(json.dumps(metrics, indent=2, ensure_ascii=False))
    print(json.dumps(metrics, indent=2, ensure_ascii=False))
    return metrics


def write_termination_metrics(
    path: Path,
    *,
    platform: str,
    task_id: str,
    stage: str,
    model: str,
) -> dict:
    """Record a final contract-shaped result when the pod receives SIGTERM."""
    try:
        out = json.loads(path.read_text())
        if not isinstance(out, dict):
            out = {}
    except Exception:  # noqa: BLE001 - this is the last-resort writer
        out = {}

    stages = ["recreation", "eval"] if stage == "recreation_eval" else [stage]
    existing = out.get("stage_outcomes")
    existing = existing if isinstance(existing, dict) else {}
    target = 0
    for index, name in enumerate(stages):
        item = existing.get(name)
        if isinstance(item, dict) and item.get("status") == "pass":
            target = min(index + 1, len(stages) - 1)
        else:
            break

    outcomes = {}
    for index, name in enumerate(stages):
        if index < target:
            outcomes[name] = existing.get(name) or {
                "status": "pass",
                "outcome_class": "completed",
                "reason_code": "completed",
                "native_exit_code": None,
                "result_complete": True,
                "retryable": False,
            }
        elif index == target:
            outcomes[name] = {
                "status": "timeout",
                "outcome_class": "terminated",
                "reason_code": "pod_terminated",
                "native_exit_code": 143,
                "result_complete": False,
                "retryable": True,
            }
        else:
            outcomes[name] = {
                "status": "not_run",
                "outcome_class": "terminated",
                "reason_code": "upstream_stage_failed",
                "native_exit_code": None,
                "result_complete": False,
                "retryable": True,
            }

    out.setdefault("task_score", 0.0)
    out.setdefault("program_score", None)
    out.setdefault("vlm_score", None)
    out.setdefault("metrics", {"status": "pod_terminated", "final_score": 0.0})
    out.update(
        {
            "task_id": out.get("task_id") or task_id,
            "model": out.get("model") or model,
            "platform": out.get("platform") or platform,
            "stage": stage,
            "passed": False,
            "pipeline_exit_code": 143,
            "process_exit_code": 143,
            "outcome_class": "terminated",
            "reason_code": "pod_terminated",
            "result_complete": False,
            "retryable": True,
            "stage_outcomes": outcomes,
        }
    )
    for name, item in outcomes.items():
        out[f"stage_{name}"] = "succeeded" if item["status"] == "pass" else "failed"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(out, indent=2))
    print(f"[rb][sigterm] wrote {path} (status=pod_terminated)")
    return out


def parse_cua_ref(ref: str) -> tuple[str, str]:
    from core.cua_driver import parse_ref

    source, version, _ = parse_ref(ref)
    return source, version


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="web.runtime")
    commands = parser.add_subparsers(dest="command", required=True)

    materialize_parser = commands.add_parser("materialize")
    materialize_parser.add_argument("instance_dir", type=Path)
    materialize_parser.add_argument("dataset_dir", type=Path)
    materialize_parser.add_argument("domain")

    metrics_parser = commands.add_parser("write-metrics")
    metrics_parser.add_argument("--output-dir", required=True, type=Path)
    metrics_parser.add_argument("--agent-rc", required=True, type=int)
    metrics_parser.add_argument("--eval-target", default="recreation")
    metrics_parser.add_argument("--scaffold", default="claude-code")

    termination_parser = commands.add_parser("write-termination")
    termination_parser.add_argument("--path", required=True, type=Path)
    termination_parser.add_argument("--platform", default="")
    termination_parser.add_argument("--task-id", default="")
    termination_parser.add_argument("--stage", default="recreation_eval")
    termination_parser.add_argument("--model", default="")

    ref_parser = commands.add_parser("parse-cua-ref")
    ref_parser.add_argument("ref")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "materialize":
        materialize(args.instance_dir, args.dataset_dir, args.domain)
    elif args.command == "write-metrics":
        write_native_metrics(
            args.output_dir,
            agent_rc=args.agent_rc,
            eval_target=args.eval_target,
            scaffold=args.scaffold,
        )
    elif args.command == "write-termination":
        write_termination_metrics(
            args.path,
            platform=args.platform,
            task_id=args.task_id,
            stage=args.stage,
            model=args.model,
        )
    elif args.command == "parse-cua-ref":
        print("|".join(parse_cua_ref(args.ref)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
