#!/usr/bin/env python3
"""Small JSON operations used by the Linux visual-evaluation shell stage."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def load_or_empty(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def merge(results_dir: Path) -> None:
    programmatic = load_or_empty(results_dir / "programmatic_results.json")
    visual = load_or_empty(results_dir / "vlm_results.json") or load_or_empty(
        results_dir / "vlm_results.partial.json"
    )
    combined = {
        "programmatic": {
            "passed": programmatic.get("passed", 0),
            "total": programmatic.get("total", 0),
            "pass_rate": programmatic.get("pass_rate", 0),
            "by_module": programmatic.get("by_module", {}),
        },
        "visual": {
            "passed": visual.get("passed", 0),
            "total": visual.get("total", 0),
            "pass_rate": visual.get("pass_rate", 0),
            "judge_errors": visual.get("judge_errors", visual.get("errors", 0)),
            "skipped": visual.get("skipped", ""),
            "error": visual.get("error", ""),
        },
    }
    (results_dir / "atspi_eval.json").write_text(json.dumps(combined, indent=2))
    print(json.dumps(combined, indent=2))


def count_manifest(path: Path) -> None:
    print(len(json.loads(path.read_text()).get("vlm_assertions", [])))


def write_skipped(results_dir: Path) -> None:
    payload = {
        "passed": 0,
        "total": 0,
        "pass_rate": 0,
        "results": {},
        "skipped": "no_assertions",
    }
    (results_dir / "vlm_results.json").write_text(json.dumps(payload, indent=2))


def apply_manifest(manifest_path: Path, assertions_path: Path) -> None:
    manifest = json.loads(manifest_path.read_text())
    fallback = load_or_empty(assertions_path)
    assertions = {}
    for item in manifest.get("vlm_assertions", []):
        if isinstance(item, str):
            assertions[item] = fallback.get(item, "")
        elif isinstance(item, dict) and item.get("name"):
            name = str(item["name"])
            assertions[name] = str(item.get("description") or fallback.get(name, ""))
    assertions_path.write_text(json.dumps(assertions, indent=2))
    print(f"VLM scored against manifest: {len(assertions)} assertions")


def count_assertions(path: Path) -> None:
    print(len(json.loads(path.read_text())))


def validate_json(path: Path) -> None:
    json.loads(path.read_text())


def write_judge_error(results_dir: Path) -> None:
    payload = {
        "passed": 0,
        "total": 0,
        "pass_rate": 0,
        "results": {},
        "error": "vlm_judge_failed",
    }
    (results_dir / "vlm_results.json").write_text(json.dumps(payload, indent=2))


def _expected_visual_total(eval_dir: Path) -> int | None:
    manifest_path = eval_dir / "test_manifest.json"
    if not manifest_path.exists():
        manifest_path = eval_dir / ".." / "tests" / "test_manifest.json"
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text())
            return int(
                manifest.get("vlm_total", 0) or len(manifest.get("vlm_assertions", []))
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            pass
    assertions_path = eval_dir / "screenshots" / "assertions.json"
    if assertions_path.exists():
        try:
            return len(json.loads(assertions_path.read_text()))
        except (OSError, TypeError, json.JSONDecodeError):
            return None
    return None


def completion(eval_dir: Path) -> None:
    try:
        programmatic = json.loads((eval_dir / "programmatic_results.json").read_text())
        visual = json.loads((eval_dir / "vlm_results.json").read_text())
    except (OSError, json.JSONDecodeError):
        print("PENDING")
        return
    assertions_path = eval_dir / "screenshots" / "assertions.json"
    expected_visual_total = None
    if assertions_path.exists():
        try:
            expected_visual_total = len(json.loads(assertions_path.read_text()))
        except (OSError, TypeError, json.JSONDecodeError):
            pass
    programmatic_total = int(programmatic.get("total", 0))
    not_run = int(
        programmatic.get("n_not_run", programmatic.get("n_injected_not_run", 0))
    )
    visual_total = int(visual.get("total", 0))
    complete = (
        programmatic_total > 0
        and not_run < programmatic_total
        and not programmatic.get("error")
        and not visual.get("error")
        and (expected_visual_total is None or visual_total == expected_visual_total)
    )
    print("DONE" if complete else "REDO")


def verify(eval_dir: Path) -> None:
    result: dict[str, object] = {
        "ok": True,
        "missing": [],
        "programmatic": None,
        "vlm": None,
    }
    expected_visual_total = _expected_visual_total(eval_dir)
    assertions_path = eval_dir / "screenshots" / "assertions.json"
    if expected_visual_total is None and assertions_path.exists():
        try:
            json.loads(assertions_path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            result["ok"] = False
            result["assertions_error"] = str(exc)
    for key, filename in (
        ("programmatic", "programmatic_results.json"),
        ("vlm", "vlm_results.json"),
    ):
        path = eval_dir / filename
        if not path.exists():
            result["ok"] = False
            result["missing"].append(filename)
            continue
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            result["ok"] = False
            result[key] = {"error": str(exc)}
            continue
        result[key] = {
            "passed": data.get("passed", 0),
            "total": data.get("total", 0),
            "pass_rate": data.get("pass_rate", 0),
            "skipped": data.get("skipped", ""),
            "error": data.get("error", ""),
            "scored_against": data.get("scored_against", "self"),
            "manifest_sha": data.get("manifest_sha", ""),
            "manifest_total": data.get("manifest_total", data.get("total", 0)),
            "n_injected_not_run": data.get("n_injected_not_run", 0),
            "n_not_run": data.get("n_not_run", data.get("n_injected_not_run", 0)),
            "n_executed": data.get("n_executed"),
            "n_unexpected": data.get("n_unexpected", 0),
            "complete": data.get("complete"),
        }
    visual = result.get("vlm")
    if isinstance(visual, dict) and expected_visual_total is not None:
        visual["expected_total"] = expected_visual_total
        if int(visual.get("total", 0)) != expected_visual_total:
            result["ok"] = False
            visual["error"] = (
                f"incomplete_vlm_results:{visual.get('total', 0)}/"
                f"{expected_visual_total}"
            )
    if isinstance(visual, dict) and visual.get("complete") is False:
        result["ok"] = False
        visual["error"] = "incomplete_vlm_checkpoint"
    print(json.dumps(result))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("merge", "write-skipped", "write-judge-error"):
        command = commands.add_parser(name)
        command.add_argument("results_dir", type=Path)
    for name in ("complete", "verify"):
        command = commands.add_parser(name)
        command.add_argument("eval_dir", type=Path)
    count = commands.add_parser("manifest-count")
    count.add_argument("manifest", type=Path)
    apply = commands.add_parser("apply-manifest")
    apply.add_argument("manifest", type=Path)
    apply.add_argument("assertions", type=Path)
    assertion_count = commands.add_parser("assertion-count")
    assertion_count.add_argument("assertions", type=Path)
    validate = commands.add_parser("validate-json")
    validate.add_argument("path", type=Path)
    args = parser.parse_args()

    if args.command == "merge":
        merge(args.results_dir)
    elif args.command == "manifest-count":
        count_manifest(args.manifest)
    elif args.command == "write-skipped":
        write_skipped(args.results_dir)
    elif args.command == "apply-manifest":
        apply_manifest(args.manifest, args.assertions)
    elif args.command == "assertion-count":
        count_assertions(args.assertions)
    elif args.command == "validate-json":
        validate_json(args.path)
    elif args.command == "complete":
        completion(args.eval_dir)
    elif args.command == "verify":
        verify(args.eval_dir)
    else:
        write_judge_error(args.results_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
