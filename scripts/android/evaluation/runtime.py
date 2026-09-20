#!/usr/bin/env python3
"""Structured-data operations used by the frozen Android evaluator wrapper."""

from __future__ import annotations

import argparse
import datetime as dt
import glob
import importlib.util
import json
from pathlib import Path


def load_manifest(path: Path) -> dict:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("test manifest must be an object")
    return value


def manifest_total(path: Path) -> int:
    try:
        return max(0, int(load_manifest(path).get("total", 0)))
    except Exception:  # noqa: BLE001 - the shell fallback historically returned zero
        return 0


def device_date(path: Path, *, encoded: bool) -> str:
    value = str(load_manifest(path).get("device_date") or "").strip()
    if not value:
        return ""
    parsed = dt.date.fromisoformat(value)
    return parsed.strftime("%m%d1200%Y.00") if encoded else value


def write_failure(path: Path, manifest: Path, reason: str) -> dict:
    total = manifest_total(manifest)
    metrics = {
        "task_score": 0.0,
        "passed": False,
        "reason": reason,
        "scored_against": "manifest",
        "total_cases": total,
        "manifest_total": total,
        "passed_cases": 0,
        "modules": {},
    }
    path.write_text(json.dumps(metrics, indent=2))
    return metrics


def _load_test_kit(tests_dir: Path):
    kit_path = tests_dir / "android_testgen_kit.py"
    spec = importlib.util.spec_from_file_location("android_testgen_kit", kit_path)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load frozen Android test kit")
    kit = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(kit)
    return kit


def aggregate(results_dir: Path, metrics_path: Path, package: str, tests_dir: Path) -> dict:
    kit = _load_test_kit(tests_dir)
    manifest = kit.load_manifest(str(tests_dir))
    if not manifest:
        raise RuntimeError("frozen test_manifest.json is invalid")
    scored = kit.score_against_manifest(str(results_dir), manifest)

    modules = {}
    for raw_path in sorted(glob.glob(str(results_dir / "android_*_eval.json"))):
        try:
            data = json.loads(Path(raw_path).read_text())
            name = data.get("module")
            total = int(data.get("total", 0))
            passed = min(int(data.get("passed", 0)), total)
        except Exception:  # noqa: BLE001 - malformed module output is ignored by contract
            continue
        if name and total > 0:
            modules[name] = {
                "total": total,
                "passed": passed,
                "pass_rate": round(passed / total, 4),
            }

    metrics = {
        "task_score": scored["pass_rate"],
        "passed": scored["pass_rate"] >= 0.6,
        "package": package,
        "scored_against": "manifest",
        "manifest_sha": scored["manifest_sha"],
        "manifest_total": scored["manifest_total"],
        "total_cases": scored["total"],
        "passed_cases": scored["passed"],
        "n_injected_not_run": scored["n_injected_not_run"],
        "n_unexpected": scored["n_unexpected"],
        "rule": scored["rule"],
        "vlm": scored["vlm"],
        "modules": modules,
        "modules_manifest": scored["by_module"],
    }
    metrics_path.write_text(json.dumps(metrics, indent=2))
    return metrics


def mark_vlm_errors(path: Path, count: int) -> dict:
    metrics = json.loads(path.read_text())
    metrics["passed"] = False
    metrics["vlm_judge_errors"] = count
    metrics["infra_error"] = "vlm_judge_errors"
    path.write_text(json.dumps(metrics, indent=2))
    return metrics


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="android.evaluation.runtime")
    commands = parser.add_subparsers(dest="command", required=True)

    total = commands.add_parser("manifest-total")
    total.add_argument("manifest", type=Path)

    date = commands.add_parser("device-date")
    date.add_argument("manifest", type=Path)
    date.add_argument("--encoded", action="store_true")

    failure = commands.add_parser("write-failure")
    failure.add_argument("--manifest", required=True, type=Path)
    failure.add_argument("--metrics", required=True, type=Path)
    failure.add_argument("--reason", required=True)

    aggregate_parser = commands.add_parser("aggregate")
    aggregate_parser.add_argument("--results-dir", required=True, type=Path)
    aggregate_parser.add_argument("--metrics", required=True, type=Path)
    aggregate_parser.add_argument("--package", required=True)
    aggregate_parser.add_argument("--tests-dir", required=True, type=Path)

    errors = commands.add_parser("mark-vlm-errors")
    errors.add_argument("--metrics", required=True, type=Path)
    errors.add_argument("--count", required=True, type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "manifest-total":
        print(manifest_total(args.manifest))
    elif args.command == "device-date":
        print(device_date(args.manifest, encoded=args.encoded), end="")
    elif args.command == "write-failure":
        print(
            json.dumps(
                write_failure(args.metrics, args.manifest, args.reason),
                ensure_ascii=False,
            )
        )
    elif args.command == "aggregate":
        print(
            "metrics.json:",
            json.dumps(
                aggregate(args.results_dir, args.metrics, args.package, args.tests_dir),
                ensure_ascii=False,
            ),
        )
    elif args.command == "mark-vlm-errors":
        mark_vlm_errors(args.metrics, args.count)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
