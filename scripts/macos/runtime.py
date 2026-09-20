#!/usr/bin/env python3
"""Structured runtime helpers shared by the macOS lifecycle scripts."""

from __future__ import annotations

import argparse
import ast
import json
import os
import shutil
import subprocess
from pathlib import Path


def record_stage(
    path: Path, stage: str, status: str, outcome: str, reason: str, native: str
) -> None:
    try:
        values = json.loads(path.read_text())
        if not isinstance(values, dict):
            values = {}
    except Exception:  # noqa: BLE001 - first write and recovery share this path
        values = {}
    values[stage] = {
        "status": status,
        "outcome_class": outcome,
        "reason_code": reason,
        "native_exit_code": int(native) if native else None,
        "result_complete": outcome == "completed",
        "retryable": outcome in ("infra_error", "terminated"),
    }
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(values, indent=2))
    os.replace(temporary, path)


def validate_instance(path: Path) -> None:
    from rb_unify.rb_instance import RBInstance

    instance = RBInstance.load(path)
    problems = [
        str(problem) for problem in instance.validate() if problem.component != "vlm"
    ]
    if instance.platform != "macos":
        problems.append(f"descriptor: expected macos, got {instance.platform!r}")
    if problems:
        raise ValueError("invalid frozen instance: " + "; ".join(problems))


def restore_build(root: Path, log_path: Path, timeout: int) -> int:
    env = os.environ.copy()
    env["APP_OUTPUT_DIR"] = str(root / "build")
    env["RB_APP_OUTPUT_DIR"] = env["APP_OUTPUT_DIR"]
    with log_path.open("a", encoding="utf-8") as log:
        try:
            result = subprocess.run(
                ["./build.sh"],
                cwd=root,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            print(f"restored build timed out after {timeout}s", file=log)
            return 143
    return result.returncode


def has_vlm_errors(path: Path) -> bool:
    result = json.loads(path.read_text())
    return int(result.get("judge_errors", result.get("errors", 0)) or 0) > 0


def desktop_mcp_config(
    driver: str, socket: str, coordinate_space: str, coordinate_scale: str
) -> dict:
    from core import mcp_settings

    env = mcp_settings.desktop_env(
        normalize=coordinate_space == "1",
        scale=coordinate_scale,
        extra={"CUA_DRIVER_RS_MCP_FORCE_PROXY": "1"},
    )
    return mcp_settings.mcp_config(
        mcp_settings.DESKTOP_SERVER,
        driver,
        ["mcp", "--socket", socket, "--no-overlay"],
        env,
    )


def render_prompt() -> str:
    from core import recreation_prompt

    return recreation_prompt.render("macos")


def verify_claude_mcp(path: Path, expected_socket: str) -> None:
    entry = (
        json.loads(path.read_text()).get("mcpServers", {}).get("desktop-control", {})
    )
    args = entry.get("args") or []
    env = entry.get("env") or {}
    if entry.get("type") != "stdio":
        raise ValueError("desktop-control is not stdio")
    if Path(entry.get("command", "")).name != "qwen-cua-driver":
        raise ValueError("desktop-control command is not qwen-cua-driver")
    if args != ["mcp", "--socket", expected_socket, "--no-overlay"]:
        raise ValueError(f"unexpected desktop-control args: {args!r}")
    if env.get("CUA_DRIVER_RS_MCP_FORCE_PROXY") != "1":
        raise ValueError("desktop-control is not fail-closed through the daemon")


def count_ax_tests(directory: Path) -> int:
    count = 0
    for path in sorted(directory.glob("test_ax_*.py")) if directory.is_dir() else []:
        try:
            tree = ast.parse(path.read_text())
        except (SyntaxError, OSError):
            continue
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.ClassDef):
                count += sum(
                    isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and item.name.startswith("test_")
                    for item in ast.iter_child_nodes(node)
                )
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                count += int(node.name.startswith("test_"))
    return count


def frozen_vlm_total(tests_dir: Path) -> int:
    candidates = (
        (tests_dir / "test_manifest.json", "vlm_assertions"),
        (tests_dir / "testcase_counts.json", "vlm_total"),
        (tests_dir.parent / "vlm_assertions.json", "assertions"),
    )
    for path, field in candidates:
        if not path.is_file():
            continue
        try:
            value = json.loads(path.read_text()).get(field)
            if isinstance(value, list):
                return len(value)
            if isinstance(value, int):
                return value
        except Exception:  # noqa: BLE001 - try the next compatibility source
            continue
    return 0


def write_zero_score(
    results_dir: Path,
    *,
    app: str,
    model: str,
    config: str,
    reason: str,
    score_reason: str,
    total: int,
    vlm_total: int,
) -> None:
    summary = {
        "app": app,
        "model": model,
        "config": config,
        "test_format": "ax_tests",
        "ax_scores": {
            "passed": 0,
            "failed": total,
            "total": total,
            "pass_rate": 0,
        },
        "vlm_scores": {"passed": 0, "total": vlm_total},
        "screenshots": {"official": 0, "recreated": 0, "assertions": 0},
        "score_reason": score_reason,
        "zero_score_reason": reason,
    }
    programmatic = {
        "passed": 0,
        "failed": total,
        "total": total,
        "pass_rate": 0,
        "test_format": "ax_tests",
        "score_reason": score_reason,
        "zero_score_reason": reason,
    }
    (results_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    (results_dir / "programmatic_results.json").write_text(
        json.dumps(programmatic, indent=2)
    )
    (results_dir / "vlm_results.json").write_text(
        json.dumps({"passed": 0, "total": vlm_total, "pass_rate": 0}, indent=2)
    )


def json_fields(path: Path, fields: list[str]) -> list[object]:
    data = json.loads(path.read_text())
    return [data.get(field, 0) for field in fields]


def accessibility_status() -> int:
    try:
        import ApplicationServices

        return 0 if ApplicationServices.AXIsProcessTrusted() else 1
    except Exception:  # noqa: BLE001 - 2 means the native API is unavailable
        return 2


def stage_vlm_assertions(
    manifest_path: Path, runtime_root: Path, output_dir: Path
) -> int:
    manifest = json.loads(manifest_path.read_text())
    output_dir.mkdir(parents=True, exist_ok=True)
    assertions = {}
    found = 0
    for item in manifest.get("vlm_assertions", []):
        if isinstance(item, str):
            name, description = item, ""
        else:
            name = str(item.get("name") or "")
            description = str(item.get("description") or "")
        if not name:
            continue
        assertions[name] = description
        matches = sorted(runtime_root.rglob(Path(name).name))
        image = next((path for path in matches if path.is_file()), None)
        if image is not None:
            target = output_dir / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(image, target)
            found += 1
    (output_dir / "assertions.json").write_text(json.dumps(assertions, indent=2))
    return found


def merge_visual_score(path: Path, passed: int, total: int) -> None:
    score = json.loads(path.read_text())
    score["visual"] = {
        "passed": passed,
        "total": total,
        "pass_rate": round(passed / total, 4) if total > 0 else 0,
    }
    path.write_text(json.dumps(score, indent=2))


def write_results(
    results_dir: Path,
    *,
    app: str,
    model: str,
    config: str,
    test_format: str,
    passed: int,
    failed: int,
    total: int,
    vlm_passed: int,
    vlm_total: int,
    official_count: int,
    recreated_count: int,
    assertion_count: int,
) -> None:
    summary = {
        "app": app,
        "model": model,
        "config": config,
        "test_format": test_format,
        "ax_scores": {
            "passed": passed,
            "failed": failed,
            "total": total,
            "pass_rate": round(passed / total * 100, 1) if total > 0 else 0,
        },
        "vlm_scores": {"passed": vlm_passed, "total": vlm_total},
        "screenshots": {
            "official": official_count,
            "recreated": recreated_count,
            "assertions": assertion_count,
        },
    }
    (results_dir / "summary.json").write_text(json.dumps(summary, indent=2))

    programmatic = {
        "passed": passed,
        "failed": failed,
        "total": total,
        "pass_rate": round(passed / total, 4) if total > 0 else 0,
        "test_format": test_format,
    }
    score_path = results_dir / "scores/score.json"
    if not score_path.exists():
        score_path = results_dir / "score.json"
    if score_path.exists():
        try:
            score = json.loads(score_path.read_text())
            if "details" in score:
                programmatic["details"] = score["details"]
        except Exception:  # noqa: BLE001 - optional detail cannot erase totals
            pass
    (results_dir / "programmatic_results.json").write_text(
        json.dumps(programmatic, indent=2)
    )

    vlm = {
        "passed": vlm_passed,
        "total": vlm_total,
        "pass_rate": round(vlm_passed / vlm_total, 4) if vlm_total > 0 else 0,
    }
    aggregate_path = results_dir / "scores/vlm_aggregate.json"
    if aggregate_path.exists():
        try:
            aggregate = json.loads(aggregate_path.read_text())
            aggregate_passed = int(aggregate.get("passed", 0) or 0)
            aggregate_total = int(aggregate.get("total", 0) or 0)
            errors = int(aggregate.get("errors", aggregate.get("judge_errors", 0)) or 0)
            vlm.update(
                {
                    "passed": aggregate_passed,
                    "total": aggregate_total,
                    "pass_rate": (
                        round(aggregate_passed / aggregate_total, 4)
                        if aggregate_total > 0
                        else 0
                    ),
                    "authored": int(
                        aggregate.get("authored", aggregate_total + errors) or 0
                    ),
                    "errors": errors,
                    "judge_errors": errors,
                    "skipped": int(aggregate.get("skipped", 0) or 0),
                    "assertion_details": aggregate,
                }
            )
            if aggregate.get("error"):
                vlm["error"] = aggregate["error"]
        except Exception:  # noqa: BLE001 - optional detail cannot erase totals
            pass
    (results_dir / "vlm_results.json").write_text(json.dumps(vlm, indent=2))


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="macos.runtime")
    commands = parser.add_subparsers(dest="command", required=True)

    stage = commands.add_parser("record-stage")
    stage.add_argument("path", type=Path)
    stage.add_argument("stage")
    stage.add_argument("status")
    stage.add_argument("outcome")
    stage.add_argument("reason")
    stage.add_argument("native", nargs="?", default="")

    instance = commands.add_parser("validate-instance")
    instance.add_argument("path", type=Path)
    commands.add_parser("check-instance-runtime")

    build = commands.add_parser("restore-build")
    build.add_argument("root", type=Path)
    build.add_argument("log", type=Path)
    build.add_argument("timeout", type=int)

    vlm_errors = commands.add_parser("has-vlm-errors")
    vlm_errors.add_argument("path", type=Path)

    mcp = commands.add_parser("mcp-config")
    mcp.add_argument("--driver", required=True)
    mcp.add_argument("--socket", required=True)
    mcp.add_argument("--coordinate-space", required=True)
    mcp.add_argument("--coordinate-scale", required=True)

    commands.add_parser("render-prompt")
    verify_mcp = commands.add_parser("verify-claude-mcp")
    verify_mcp.add_argument("path", type=Path)
    verify_mcp.add_argument("--socket", required=True)

    count_tests = commands.add_parser("count-ax-tests")
    count_tests.add_argument("directory", type=Path)
    vlm_total = commands.add_parser("vlm-total")
    vlm_total.add_argument("directory", type=Path)

    zero = commands.add_parser("write-zero-score")
    zero.add_argument("--results-dir", required=True, type=Path)
    zero.add_argument("--app", default="")
    zero.add_argument("--model", default="")
    zero.add_argument("--config", default="")
    zero.add_argument("--reason", required=True)
    zero.add_argument("--score-reason", required=True)
    zero.add_argument("--total", required=True, type=int)
    zero.add_argument("--vlm-total", required=True, type=int)

    fields = commands.add_parser("json-fields")
    fields.add_argument("path", type=Path)
    fields.add_argument("fields", nargs="+")

    commands.add_parser("accessibility-status")

    vlm_stage = commands.add_parser("stage-vlm")
    vlm_stage.add_argument("manifest", type=Path)
    vlm_stage.add_argument("runtime_root", type=Path)
    vlm_stage.add_argument("output_dir", type=Path)

    visual = commands.add_parser("merge-visual")
    visual.add_argument("path", type=Path)
    visual.add_argument("passed", type=int)
    visual.add_argument("total", type=int)

    percentage = commands.add_parser("percentage")
    percentage.add_argument("passed", type=int)
    percentage.add_argument("total", type=int)

    results = commands.add_parser("write-results")
    results.add_argument("--results-dir", required=True, type=Path)
    results.add_argument("--app", default="")
    results.add_argument("--model", default="")
    results.add_argument("--config", default="")
    results.add_argument("--test-format", default="ax_tests")
    results.add_argument("--passed", required=True, type=int)
    results.add_argument("--failed", required=True, type=int)
    results.add_argument("--total", required=True, type=int)
    results.add_argument("--vlm-passed", required=True, type=int)
    results.add_argument("--vlm-total", required=True, type=int)
    results.add_argument("--official-count", required=True, type=int)
    results.add_argument("--recreated-count", required=True, type=int)
    results.add_argument("--assertion-count", required=True, type=int)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "record-stage":
        record_stage(
            args.path,
            args.stage,
            args.status,
            args.outcome,
            args.reason,
            args.native,
        )
    elif args.command == "check-instance-runtime":
        from rb_unify.rb_instance import RBInstance  # noqa: F401
    elif args.command == "validate-instance":
        validate_instance(args.path)
    elif args.command == "restore-build":
        return restore_build(args.root, args.log, args.timeout)
    elif args.command == "has-vlm-errors":
        return 0 if has_vlm_errors(args.path) else 1
    elif args.command == "mcp-config":
        print(
            json.dumps(
                desktop_mcp_config(
                    args.driver,
                    args.socket,
                    args.coordinate_space,
                    args.coordinate_scale,
                )
            )
        )
    elif args.command == "render-prompt":
        print(render_prompt())
    elif args.command == "verify-claude-mcp":
        verify_claude_mcp(args.path, args.socket)
    elif args.command == "count-ax-tests":
        print(count_ax_tests(args.directory))
    elif args.command == "vlm-total":
        print(frozen_vlm_total(args.directory))
    elif args.command == "write-zero-score":
        write_zero_score(
            args.results_dir,
            app=args.app,
            model=args.model,
            config=args.config,
            reason=args.reason,
            score_reason=args.score_reason,
            total=args.total,
            vlm_total=args.vlm_total,
        )
    elif args.command == "json-fields":
        print(" ".join(str(value) for value in json_fields(args.path, args.fields)))
    elif args.command == "accessibility-status":
        print(accessibility_status())
    elif args.command == "stage-vlm":
        print(stage_vlm_assertions(args.manifest, args.runtime_root, args.output_dir))
    elif args.command == "merge-visual":
        merge_visual_score(args.path, args.passed, args.total)
    elif args.command == "percentage":
        print(round(args.passed / args.total * 100, 1) if args.total > 0 else 0)
    elif args.command == "write-results":
        write_results(
            args.results_dir,
            app=args.app,
            model=args.model,
            config=args.config,
            test_format=args.test_format,
            passed=args.passed,
            failed=args.failed,
            total=args.total,
            vlm_passed=args.vlm_passed,
            vlm_total=args.vlm_total,
            official_count=args.official_count,
            recreated_count=args.recreated_count,
            assertion_count=args.assertion_count,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
