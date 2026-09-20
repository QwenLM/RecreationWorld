#!/usr/bin/env python3
"""RecreationBench macOS AX test runner — runs each test function independently.

Each test function in test_ax_*.py files is run as an independent subprocess
with its own timeout. This ensures a single hanging test doesn't block others.

The conftest.py (deployed by stage2) provides pytest fixtures (ax, app_process,
app_path) that handle app lifecycle automatically.

Usage:
    python3 ax_test_runner.py \
        --app-path /path/to/App.app \
        --tests /path/to/test_dir \
        --results /path/to/results_dir \
        --summary results.json
"""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
import subprocess
import sys

# jump_kind -> tier (mirrors rb_testgen_kit.JUMP_KINDS; inlined so the runner stays
# self-contained when uploaded to the target).
_RB_TIER = {"none": 0, "liveness": 1, "reach": 2, "transition": 2, "value": 3}
# 120s, not 30s: a test fixture relaunches the app and waits for its AX tree to be ready
# (conftest allows 45s, which heavy Qt/Electron apps need), so a 30s cap kills such a test
# mid-setup and records a failure that says nothing about the recreation.
DEFAULT_PER_TEST_TIMEOUT = int(os.environ.get("RB_EVAL_TEST_TIMEOUT", "120"))


def find_test_files(test_dir: str) -> list[str]:
    files = []
    for f in sorted(os.listdir(test_dir)):
        if f.startswith("test_ax_") and f.endswith(".py"):
            files.append(os.path.join(test_dir, f))
    return files


def extract_test_functions(test_file: str) -> list[tuple[str, str]]:
    """Extract (class_name, func_name) pairs from a test file using AST."""
    try:
        with open(test_file) as f:
            tree = ast.parse(f.read())
    except (SyntaxError, OSError):
        return []

    tests = []
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            for item in ast.iter_child_nodes(node):
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    if item.name.startswith("test_"):
                        tests.append((node.name, item.name))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("test_"):
                tests.append(("", node.name))
    return tests


def run_single_test(
    test_file: str,
    class_name: str,
    func_name: str,
    env: dict,
    timeout: int = DEFAULT_PER_TEST_TIMEOUT,
) -> dict:
    """Run a single test function via pytest and return result."""
    if class_name:
        node_id = f"{test_file}::{class_name}::{func_name}"
    else:
        node_id = f"{test_file}::{func_name}"

    # Short node_id for display
    display_id = (
        f"{os.path.basename(test_file)}::{class_name}::{func_name}"
        if class_name
        else f"{os.path.basename(test_file)}::{func_name}"
    )

    # Per-test record file: conftest's pytest_runtest_makereport writes the
    # @pytest.mark.rb(depth=, jump_kind=, expect=) declaration + outcome here. We read
    # it back for depth/jump metadata (pass/fail stays authoritative from the exit code).
    rb_base = env.get("RESULTS_DIR") or os.path.dirname(test_file)
    rb_dir = os.path.join(rb_base, "_rb")
    rb_file = os.path.join(
        rb_dir, re.sub(r"[^A-Za-z0-9_.-]", "_", f"{class_name}__{func_name}") + ".json"
    )
    try:
        os.makedirs(rb_dir, exist_ok=True)
        if os.path.exists(rb_file):
            os.remove(rb_file)
    except Exception:
        pass
    env = {**env, "RB_RECORD_FILE": rb_file}

    cmd = [sys.executable, "-m", "pytest", node_id, "-x", "--tb=short", "-q", "-rs"]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
            cwd=os.path.dirname(test_file),
        )
        passed = result.returncode == 0
        error = ""
        output = result.stdout + result.stderr
        if passed and re.search(r"\d+\s+skipped", output):
            passed = False
            skip_lines = [line for line in output.strip().split("\n") if "SKIP" in line.upper()]
            error = skip_lines[0].strip()[:200] if skip_lines else "skipped"
        elif not passed:
            lines = output.strip().split("\n")
            error_lines = [
                line
                for line in lines
                if "FAILED" in line or "Error" in line or "assert" in line.lower()
            ]
            if not error_lines:
                error_lines = lines[-5:]
            error = "\n".join(error_lines[-5:])[:500]
    except subprocess.TimeoutExpired:
        passed = False
        error = f"TIMEOUT after {timeout}s"
    except Exception as e:
        passed = False
        error = str(e)[:500]

    # Fold in the declared depth/jump metadata (if the test used @pytest.mark.rb).
    # Default to depth=0/jump_kind="none" so tests without the marker stay valid.
    depth, jump_kind, tier, expect = 0, "none", 0, ""
    try:
        if os.path.exists(rb_file):
            rd = json.load(open(rb_file))
            depth = max(0, int(rd.get("depth", 0) or 0))
            jk = rd.get("jump_kind", "none") or "none"
            jump_kind = jk if jk in _RB_TIER else "none"
            tier = _RB_TIER.get(jump_kind, 0)
            expect = str(rd.get("expect", "") or "")
    except Exception:
        pass

    return {
        "name": display_id,
        "passed": passed,
        "error": error,
        "depth": depth,
        "jump_kind": jump_kind,
        "tier": tier,
        "expect": expect,
    }


def run_test_module(
    test_file: str,
    env: dict,
    results_dir: str,
    per_test_timeout: int = DEFAULT_PER_TEST_TIMEOUT,
) -> dict:
    """Run all test functions in a module, each with independent timeout."""
    module_name = os.path.basename(test_file).replace("test_ax_", "").replace(".py", "")
    module_results_dir = os.path.join(results_dir, module_name)
    os.makedirs(module_results_dir, exist_ok=True)

    # Set RESULTS_DIR for this module (used by screenshot helpers in test)
    module_env = env.copy()
    module_env["RESULTS_DIR"] = module_results_dir

    test_funcs = extract_test_functions(test_file)
    if not test_funcs:
        return {"module": module_name, "tests": [], "error": "no test functions found"}

    tests = []
    for class_name, func_name in test_funcs:
        result = run_single_test(
            test_file, class_name, func_name, module_env, per_test_timeout
        )
        tests.append(result)
        status = "PASS" if result["passed"] else "FAIL"
        suffix = ""
        if result["error"]:
            first_line = result["error"].split("\n")[0][:60]
            suffix = f" ({first_line})"
        print(
            f"    {status}: {class_name}::{func_name}{suffix}"
            if class_name
            else f"    {status}: {func_name}{suffix}"
        )

    data = {"module": module_name, "tests": tests}

    result_file = os.path.join(module_results_dir, f"{module_name}_results.json")
    with open(result_file, "w") as f:
        json.dump(data, f, indent=2)

    return data


def collect_summary(by_module: dict) -> dict:
    total_passed = 0
    total_tests = 0
    for module_name, data in by_module.items():
        tests = data.get("tests", [])
        for t in tests:
            total_tests += 1
            if t.get("passed", False):
                total_passed += 1

    pass_rate = round(total_passed / total_tests, 4) if total_tests > 0 else 0
    return {
        "passed": total_passed,
        "total": total_tests,
        "pass_rate": pass_rate,
        "by_module": {
            name: {
                "passed": sum(1 for t in d.get("tests", []) if t.get("passed")),
                "total": len(d.get("tests", [])),
                "error": d.get("error", ""),
            }
            for name, d in by_module.items()
        },
    }


def main():
    parser = argparse.ArgumentParser(description="RecreationBench AX test runner")
    parser.add_argument("--app-path", required=True, help="Path to .app bundle")
    parser.add_argument(
        "--tests", required=True, help="Directory with test_ax_*.py files"
    )
    parser.add_argument("--results", required=True, help="Results output directory")
    parser.add_argument("--fixtures", default="", help="Fixtures directory")
    parser.add_argument(
        "--summary", default="ax_results.json", help="Summary JSON filename"
    )
    parser.add_argument("--fail-under", type=float, default=0, help="Minimum pass rate")
    parser.add_argument("--app-name", default="", help="App name for AX lookup")
    parser.add_argument(
        "--per-test-timeout",
        type=int,
        default=DEFAULT_PER_TEST_TIMEOUT,
        help="Per-test-function timeout in seconds (env: RB_EVAL_TEST_TIMEOUT)",
    )
    parser.add_argument("--print-summary", action="store_true")
    args = parser.parse_args()

    os.makedirs(args.results, exist_ok=True)

    test_files = find_test_files(args.tests)
    if not test_files:
        print("ERROR: No test_ax_*.py files found")
        summary = {"passed": 0, "total": 0, "pass_rate": 0, "error": "no_test_files"}
        with open(os.path.join(args.results, args.summary), "w") as f:
            json.dump(summary, f, indent=2)
        sys.exit(1)

    # Build env for subprocesses — conftest.py uses APP_PATH and APP_NAME
    env = os.environ.copy()
    env["APP_PATH"] = args.app_path
    if args.app_name:
        env["APP_NAME"] = args.app_name
    if args.fixtures:
        env["FIXTURES_DIR"] = args.fixtures

    print(
        f"Running {len(test_files)} test modules for {args.app_name or args.app_path}"
    )
    by_module = {}

    for test_file in test_files:
        module_name = (
            os.path.basename(test_file).replace("test_ax_", "").replace(".py", "")
        )
        test_funcs = extract_test_functions(test_file)
        print(f"\n--- Module: {module_name} ({len(test_funcs)} tests) ---")

        data = run_test_module(test_file, env, args.results, args.per_test_timeout)
        by_module[module_name] = data

        passed = sum(1 for t in data.get("tests", []) if t.get("passed"))
        total = len(data.get("tests", []))
        print(f"  Module result: {passed}/{total}")

    summary = collect_summary(by_module)

    summary_path = os.path.join(args.results, args.summary)
    with open(summary_path, "w") as f:
        json.dump(summary, f, indent=2)

    if args.print_summary:
        print(
            f"\n=== Summary: {summary['passed']}/{summary['total']} "
            f"({summary['pass_rate']*100:.0f}%) ==="
        )
        for mod, info in summary["by_module"].items():
            print(
                f"  {mod}: {info['passed']}/{info['total']}"
                + (f" error={info['error']}" if info.get("error") else "")
            )

    if args.fail_under > 0 and summary["pass_rate"] < args.fail_under:
        print(
            f"\nFAIL: pass rate {summary['pass_rate']*100:.0f}% "
            f"below threshold {args.fail_under*100:.0f}%"
        )
        sys.exit(1)

    sys.exit(0)


if __name__ == "__main__":
    main()
