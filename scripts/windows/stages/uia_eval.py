"""Stage: UIA evaluation with VLM visual assertions.

Runs UIA test scripts against build/recreation, collects programmatic
results + screenshots, then judges visual assertions with a VLM.

Two-phase design (both run locally on Windows):
  Phase 1: Run test scripts → results + screenshots
  Phase 2: Judge screenshots with VLM

Mirrors scripts/linux/stages/atspi_eval.py but executes locally
on a Windows machine via subprocess instead of inside Docker containers.
"""

from __future__ import annotations

import glob
import ast as _ast
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

if __package__:
    from .config import vm_task_dir
else:  # Support the documented `python uia_eval.py ...` entrypoint.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from stages.config import vm_task_dir

# scripts/core is synced to the VM by deploy_and_test.py alongside scripts/windows.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from common import vlm_judge as shared_vlm_judge  # noqa: E402
from core import desktop_capture as core_desktop_capture  # noqa: E402
from core import manifest as core_manifest  # noqa: E402

# How long each test module gets to run
MODULE_TIMEOUT = 600


def _bring_app_to_foreground(pid: int | None) -> None:
    """Bring the app window to the foreground before running tests/screenshots.

    Without this, ImageGrab.grab() may capture a blank/desktop image if another
    process stole focus between launch and test execution.
    """
    if not pid:
        return
    try:
        import psutil
        from pywinauto import Application

        pids = {pid}
        try:
            parent = psutil.Process(pid)
            for child in parent.children(recursive=True):
                pids.add(child.pid)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

        for p in pids:
            try:
                app = Application(backend="uia").connect(process=p)
                win = app.top_window()
                if win.is_minimized():
                    win.restore()
                win.set_focus()
                return
            except Exception:
                continue
    except Exception:
        pass


def _dismiss_stray_windows(exe_path: str) -> None:
    """Close system dialogs and stray windows before launching the app."""
    try:
        import psutil
        from pywinauto import Desktop

        target_exe = os.path.basename(exe_path).lower()
        for w in Desktop(backend="uia").windows():
            try:
                try:
                    pid = w.process_id()
                    proc = psutil.Process(pid)
                    if proc.name().lower() == target_exe:
                        continue
                except Exception:
                    pass
                title = w.window_text()
                if not title or title in (
                    "",
                    "Program Manager",
                    "Microsoft Text Input Application",
                ):
                    continue
                if title in (
                    "Shutdown Event Tracker",
                    "Windows Error Reporting",
                    "Microsoft Visual C++ Runtime Library",
                ):
                    try:
                        w.close()
                    except Exception:
                        pass
            except Exception:
                continue
    except ImportError:
        pass


def _capture_screenshot(results_dir: str, name: str) -> None:
    """Capture the desktop into the results' screenshots dir, reporting rather than raising."""
    print(
        "    Screenshot: "
        + core_desktop_capture.capture_desktop(
            os.path.join(results_dir, "screenshots", name)
        )
    )


def _find_test_files(tests_dir: str) -> list[Path]:
    """Find all test_uia_*.py files in the given directory and tests/ subdir."""
    tests = []
    for pattern in ("test_uia_*.py", "tests/test_uia_*.py"):
        tests.extend(Path(tests_dir).glob(pattern))
    return sorted({p.resolve() for p in tests if p.is_file()})


def _module_name(path: Path) -> str:
    """Extract module name from test file path."""
    name = path.stem
    return name.removeprefix("test_uia_")


def _is_module_match(candidate: str, module: str) -> bool:
    """Check if candidate name matches module (handles singular/plural variants)."""
    return (
        candidate == module
        or candidate == module.rstrip("s")
        or module.startswith(candidate)
    )


def _count_expected_tests(test_file: Path) -> int | None:
    """Count expected test functions in a pytest test file.

    Uses AST analysis to count `def test_*` functions and parametrize decorators.
    Returns None if the file cannot be parsed.
    """
    try:
        source = test_file.read_text(encoding="utf-8-sig")
        tree = _ast.parse(source, filename=str(test_file))
    except Exception:
        return None

    count = 0
    for node in _ast.walk(tree):
        if isinstance(node, _ast.FunctionDef) and node.name.startswith("test_"):
            # Check for parametrize decorator
            params = 1
            for deco in node.decorator_list:
                if isinstance(deco, _ast.Call):
                    fn = deco.func
                    fname = ""
                    if isinstance(fn, _ast.Attribute):
                        fname = fn.attr
                    elif isinstance(fn, _ast.Name):
                        fname = fn.id
                    if "parametrize" in fname and deco.args:
                        # Try to count parametrize values
                        vals = deco.args[1] if len(deco.args) > 1 else None
                        if vals and isinstance(vals, (_ast.List, _ast.Tuple)):
                            params = len(vals.elts)
            count += params
    return count if count > 0 else None


if __package__:
    from .app_lifecycle import (
        kill_app as _kill_app,
    )
    from .app_lifecycle import (
        launch_app as _launch_app_lifecycle,
    )
else:
    from stages.app_lifecycle import (
        kill_app as _kill_app,
    )
    from stages.app_lifecycle import (
        launch_app as _launch_app_lifecycle,
    )


def _launch_app(
    exe_path: str, retries: int = 3, launch_ps1: str | None = None
) -> tuple[None, int | None]:
    """Launch app with retries. Wrapper around app_lifecycle.launch_app for compatibility.

    Returns (None, pid) or (None, None). The first element is always None
    (legacy interface; previously could return a Popen object).
    """
    pid, _ = _launch_app_lifecycle(exe_path, launch_ps1=launch_ps1, retries=retries)
    return None, pid


def _detect_app_name(pid: int, exe_path: str, timeout: float = 15.0) -> str:
    """Detect the app's window title or process name after launch.

    Tries to find the main window of the launched process (or its children) and
    returns its title. Falls back to the exe filename without extension. This is
    the Windows equivalent of Linux's AT-SPI detect_app_name: the returned name
    is what rb_uia.get_app() will match against, making tests path-independent.
    """
    import time

    exe_name = os.path.splitext(os.path.basename(exe_path))[0] if exe_path else ""
    if not pid:
        return exe_name
    deadline = time.time() + timeout

    try:
        import psutil
    except ImportError:
        return exe_name

    while time.time() < deadline:
        try:
            # Collect the process tree (the launched PID might be a wrapper)
            pids = {pid}
            try:
                parent = psutil.Process(pid)
                for child in parent.children(recursive=True):
                    pids.add(child.pid)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass

            # Find windows belonging to any process in the tree
            from pywinauto import Desktop

            for w in Desktop(backend="uia").windows():
                try:
                    if w.process_id() in pids:
                        title = (w.window_text() or "").strip()
                        if title:
                            return title
                except Exception:
                    continue
        except Exception:
            pass
        time.sleep(0.5)

    return exe_name


def _run_test_module(
    test_file: Path,
    app_identifier: str,
    results_dir: str,
    fixtures_dir: str,
    launch_ps1: str = "",
) -> dict:
    """Run a single test module via pytest and return its result summary.

    Uses pytest with conftest.py for automatic result collection. The conftest
    launches a FRESH app instance per test and kills it after (per-test isolation).
    """
    module = _module_name(test_file)
    env = os.environ.copy()
    env["FIXTURES_DIR"] = fixtures_dir
    # Extract exe stem for window-title matching
    if (
        os.sep in app_identifier
        or "/" in app_identifier
        or app_identifier.lower().endswith(".exe")
    ):
        env["RB_APP_NAME"] = os.path.splitext(os.path.basename(app_identifier))[0]
        env["RB_APP_BINARY"] = app_identifier
    else:
        env["RB_APP_NAME"] = app_identifier
    # Tell conftest how to launch the app
    if launch_ps1:
        env["RB_LAUNCH_SCRIPT"] = launch_ps1
    env["RB_RESULTS_DIR"] = results_dir
    # Force UTF-8 to prevent UnicodeDecodeError on non-ASCII app output
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    # Ensure conftest.py and rb_uia.py are importable from the test dir
    test_dir = str(test_file.parent)
    env["PYTHONPATH"] = test_dir + os.pathsep + env.get("PYTHONPATH", "")

    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                str(test_file),
                "--rootdir",
                test_dir,
                "--timeout",
                str(MODULE_TIMEOUT),
                "-q",
                "-s",
                "--tb=short",
                "-o",
                "addopts=",
            ],
            capture_output=True,
            text=True,
            timeout=MODULE_TIMEOUT + 30,
            env=env,
            encoding="utf-8",
            errors="replace",
        )
        for line in proc.stdout.splitlines()[-8:]:
            print(f"    {line}")
        if proc.stderr:
            for line in proc.stderr.splitlines()[-4:]:
                print(f"    [err] {line}")
    except subprocess.TimeoutExpired:
        print(f"    TIMEOUT: {module} exceeded {MODULE_TIMEOUT}s")
        return {
            "module": module,
            "module_error": True,
            "tests": [
                {
                    "name": f"{module}_timeout",
                    "passed": False,
                    "message": "test timed out",
                }
            ],
        }
    except Exception as e:
        print(f"    ERROR running {module}: {e}")
        return {
            "module": module,
            "module_error": True,
            "tests": [{"name": f"{module}_error", "passed": False, "message": str(e)}],
        }

    result_path = Path(results_dir) / f"{module}_results.json"
    if result_path.exists():
        try:
            data = json.loads(result_path.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                return data
            if isinstance(data, list):
                return {
                    "module": module,
                    "tests": data,
                    "passed": sum(1 for t in data if t.get("passed")),
                    "total": len(data),
                }
        except (json.JSONDecodeError, Exception):
            pass

    # Check for variant naming (e.g., test writes "button_results" for module "buttons")
    for candidate in Path(results_dir).glob("*_results.json"):
        if candidate.name in (
            "programmatic_results.json",
            "vlm_results.json",
            "vlm_results.partial.json",
        ):
            continue
        stem = candidate.stem.removesuffix("_results")
        if _is_module_match(stem, module):
            try:
                data = json.loads(candidate.read_text(encoding="utf-8-sig"))
                if isinstance(data, dict):
                    return data
                if isinstance(data, list):
                    return {
                        "module": module,
                        "tests": data,
                        "passed": sum(1 for t in data if t.get("passed")),
                        "total": len(data),
                    }
            except (json.JSONDecodeError, Exception):
                pass

    return {
        "module": module,
        "module_error": True,
        "tests": [
            {
                "name": f"{module}_no_output",
                "passed": False,
                "message": "test produced no result json",
            }
        ],
    }


def _collect_summary(results_dir: str, test_files: list[Path]) -> dict:
    """Collect and summarize all module results.

    Deduplicates variant-named result files (e.g. menu vs menus) by
    matching against canonical module names from test_files.

    Uses AST-based expected test counts to detect missing tests from
    crashed modules — missing tests are counted as failures so that
    pass_rate denominators are not artificially reduced.
    """
    expected_modules = [_module_name(f) for f in test_files]

    # Pre-compute expected test counts per module via AST analysis
    expected_counts: dict[str, int | None] = {}
    for f in test_files:
        module = _module_name(f)
        expected_counts[module] = _count_expected_tests(f)

    by_module: dict[str, dict[str, int]] = {}
    total_pass = 0
    total_tests = 0
    total_actual = 0
    total_missing = 0
    seen: set[str] = set()

    for path in sorted(Path(results_dir).glob("*_results.json")):
        if path.name in (
            "programmatic_results.json",
            "vlm_results.json",
            "vlm_results.partial.json",
        ):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, OSError):
            continue

        if isinstance(data, list):
            tests = data
        elif isinstance(data, dict):
            tests = data.get("tests") or data.get("results", [])
        else:
            continue
        if not isinstance(tests, list) or not tests:
            continue

        raw_name = path.stem.removesuffix("_results")

        # Map to canonical module name from test_files
        canonical = raw_name
        for expected in expected_modules:
            if _is_module_match(raw_name, expected):
                canonical = expected
                break

        if canonical in seen:
            continue
        seen.add(canonical)

        passed = sum(1 for t in tests if isinstance(t, dict) and t.get("passed", False))
        actual = len(tests)
        exp = expected_counts.get(canonical)
        effective_total = max(actual, exp) if exp is not None else actual
        missing = effective_total - actual

        entry: dict = {"passed": passed, "total": effective_total}
        if actual != effective_total:
            entry["actual_total"] = actual
        if exp is not None:
            entry["expected"] = exp
        if missing > 0:
            entry["missing"] = missing

        by_module[canonical] = entry
        total_pass += passed
        total_tests += effective_total
        total_actual += actual
        total_missing += missing

    # Modules that had no result file at all (complete crash before any output)
    for module in expected_modules:
        if module in seen:
            continue
        exp = expected_counts.get(module)
        if exp is not None and exp > 0:
            by_module[module] = {
                "passed": 0,
                "total": exp,
                "actual_total": 0,
                "expected": exp,
                "missing": exp,
            }
            total_tests += exp
            total_missing += exp
            seen.add(module)

    result: dict = {
        "passed": total_pass,
        "total": total_tests,
        "pass_rate": round(total_pass / total_tests, 4) if total_tests else 0,
        "by_module": by_module,
        "runner": {
            "test_files": len(test_files),
            "result_files": len(by_module),
        },
    }
    if total_actual != total_tests:
        result["actual_total"] = total_actual
    if total_missing > 0:
        result["missing_tests"] = total_missing

    return result


# ── Manifest scoring (fixed denominator) ─────────────────────────────────────


def _iter_result_statuses(results_dir: str) -> dict[str, str]:
    """Collect ``{module::test: status}`` — delegated to core.manifest.

    This used to be a local reimplementation of the same parse. core's version is a strict
    superset: it also accepts ``tests`` as a dict, keeps ``skipped``/``not_run``/``error``
    distinct instead of collapsing every non-pass to "failed", and skips the same
    aggregate files (SKIP_PREFIXES covers programmatic*/vlm*/baseline_results*).

    Behaviour here is unchanged: the two callers already binarise with
    ``elif status != "passed": status = "failed"``, and pass_rate counts only "passed", so
    the richer vocabulary cannot move a score. core reads with utf-8-sig for the BOMs
    windows writes.
    """
    return core_manifest.iter_result_statuses(Path(results_dir))


def score_against_manifest(results_dir: str, manifest: dict) -> dict:
    """Score a candidate run against a frozen manifest (denominator fixed).

    Reads the existing programmatic_results.json, then re-scores using only
    tests in the manifest. The original structure is preserved; scoring fields
    (passed, total, pass_rate, by_module) are overwritten.
    """
    # Read original results to preserve structure
    prog_path = os.path.join(results_dir, "programmatic_results.json")
    original = {}
    try:
        original = json.loads(Path(prog_path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        pass

    statuses = _iter_result_statuses(results_dir)
    expected = manifest.get("tests", []) or []
    per_test: dict[str, str] = {}
    by_module: dict[str, dict[str, int]] = {}
    passed = 0
    injected = 0
    total = len(expected)
    for t in expected:
        qn = t["name"]
        module = t.get("module") or qn.split("::", 1)[0]
        status = statuses.get(qn)
        if status is None:
            status = "not_run"
            injected += 1
        elif status != "passed":
            status = "failed"
        per_test[qn] = status
        bucket = by_module.setdefault(module, {"passed": 0, "total": 0})
        bucket["total"] += 1
        if status == "passed":
            passed += 1
            bucket["passed"] += 1

    original.update(
        {
            "passed": passed,
            "total": total,
            "pass_rate": round(passed / total, 4) if total else 0,
            "by_module": by_module,
            "scored_against": "manifest",
            "manifest_sha": manifest.get("manifest_sha", ""),
            "injected_not_run": injected,
            "per_test": per_test,
        }
    )
    return original


def _read_vlm_assertions(results_dir: str) -> dict[str, str]:
    """Read VLM assertions from jsonl (preferred) and assertions.json."""
    ss_dir = os.path.join(results_dir, "screenshots")
    assertions = {}
    jsonl_path = os.path.join(ss_dir, "screenshot_asserts.jsonl")
    if os.path.exists(jsonl_path):
        try:
            with open(jsonl_path, encoding="utf-8-sig") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    entry = json.loads(line)
                    assertions[entry["screenshot"]] = entry["description"]
        except Exception:
            pass
    json_path = os.path.join(ss_dir, "assertions.json")
    if os.path.exists(json_path):
        try:
            data = json.loads(Path(json_path).read_text(encoding="utf-8-sig"))
            for k, v in data.items():
                if k not in assertions:
                    assertions[k] = v
        except Exception:
            pass
    return assertions


def score_vlm_against_manifest(results_dir: str, manifest: dict) -> dict | None:
    """Re-score VLM results against a frozen manifest (denominator fixed).

    Reads the existing vlm_results.json, then re-scores using only assertions
    in the manifest. The original structure is preserved; scoring fields are
    overwritten. Returns None if manifest has no VLM assertions.
    """
    manifest_vlm = manifest.get("vlm_assertions", [])
    if not manifest_vlm:
        return None

    vlm_path = os.path.join(results_dir, "vlm_results.json")
    if not os.path.exists(vlm_path):
        return None

    try:
        original = json.loads(Path(vlm_path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return None

    candidate_results = original.get("results", {})

    total = len(manifest_vlm)
    passed = 0
    injected = 0
    judge_errors = 0
    missing_screenshots = 0
    scored_results = {}

    for item in manifest_vlm:
        # Unified frozen manifests carry structured assertions
        # (``{"name", "description"}``); legacy manifests used bare filename
        # strings.  Score both shapes by screenshot filename.  Iterating the
        # structured object directly used to pass a dict to ``in`` below and
        # crash after an otherwise successful Windows reference evaluation.
        if isinstance(item, dict):
            fname = str(item.get("name") or item.get("screenshot") or "")
        else:
            fname = str(item or "")
        if not fname:
            raise ValueError("VLM manifest assertion is missing its name")
        if fname in candidate_results:
            result = candidate_results[fname]
            scored_results[fname] = result
            if result.get("pass", False):
                passed += 1
            if result.get("error"):
                judge_errors += 1
            if result.get("missing"):
                missing_screenshots += 1
        else:
            injected += 1
            scored_results[fname] = {
                "pass": False,
                "reason": "not_run (missing from candidate)",
            }

    judge_error_rate = round(judge_errors / total, 4) if total else 0

    original.update(
        {
            "passed": passed,
            "total": total,
            "pass_rate": round(passed / total, 4) if total else 0,
            "results": scored_results,
            "judge_errors": judge_errors,
            "judge_error_rate": judge_error_rate,
            "missing_screenshots": missing_screenshots,
            "scored_against": "manifest",
            "injected_not_run": injected,
        }
    )
    return original


def run_phase1(
    task_id: str = "",
    exe_path: str = "",
    variant: str = "recreation",
    tests_dir: str | None = None,
    fixtures_dir: str | None = None,
    results_dir: str | None = None,
    launch_ps1: str | None = None,
) -> dict:
    """Phase 1: Run UIA test scripts against the app.

    Args:
        task_id: Task identifier (used to derive paths if results_dir not given)
        exe_path: Path to the executable to test
        variant: recreation variant name
        tests_dir: Directory containing the frozen test suite
        fixtures_dir: Directory containing test fixtures
        results_dir: Explicit results output directory (overrides task_id-based path)
        launch_ps1: Path to launch.ps1 script (preferred over direct exe launch)

    Returns:
        Summary dict with programmatic results
    """
    if results_dir is None:
        base = vm_task_dir(task_id)
        if tests_dir is None:
            tests_dir = os.path.join(base, "tests")
        results_dir = os.path.join(base, f"uia_eval_{variant}")
    elif tests_dir is None:
        tests_dir = results_dir

    os.makedirs(results_dir, exist_ok=True)

    # Clean stale results
    for pattern in (
        "*_results.json",
        "*_eval.json",
        "programmatic_results.json",
        "vlm_results.json",
        "vlm_results.partial.json",
        "uia_eval.json",
    ):
        for f in glob.glob(os.path.join(results_dir, pattern)):
            os.remove(f)
    screenshots_dir = os.path.join(results_dir, "screenshots")
    if os.path.exists(screenshots_dir):
        shutil.rmtree(screenshots_dir)

    # Copy test files and helper modules from the frozen suite.
    for f in Path(tests_dir).glob("*.py"):
        shutil.copy2(f, results_dir)
    tests_subdir = Path(tests_dir) / "tests"
    if tests_subdir.exists():
        for f in tests_subdir.glob("*.py"):
            shutil.copy2(f, results_dir)
    manifest_src = Path(tests_dir) / "test_manifest.json"
    if manifest_src.exists():
        shutil.copy2(manifest_src, results_dir)

    # Copy fixtures
    if fixtures_dir is None:
        fixtures_dir = os.path.join(tests_dir, "fixtures")
    eval_fixtures = os.path.join(results_dir, "fixtures")
    if os.path.exists(eval_fixtures):
        shutil.rmtree(eval_fixtures)
    if os.path.exists(fixtures_dir):
        shutil.copytree(fixtures_dir, eval_fixtures)
    else:
        os.makedirs(eval_fixtures, exist_ok=True)

    test_files = _find_test_files(results_dir)
    test_count = len(test_files)
    print(f"  Test files: {test_count}")
    print(f"  Fixtures: {len(list(Path(eval_fixtures).rglob('*')))}")

    if test_count == 0:
        error_result = {
            "error": "no test files",
            "passed": 0,
            "total": 0,
            "pass_rate": 0,
        }
        Path(os.path.join(results_dir, "programmatic_results.json")).write_text(
            json.dumps(error_result, indent=2), encoding="utf-8"
        )
        return error_result

    # Kill any existing instances before starting
    _kill_app(exe_path)

    # One launch purely to photograph the app, then kill it again: every module screenshot below
    # is taken between two conftest-managed instances, so it shows an empty desktop and cannot
    # answer whether the recreation renders at all.  A launch failure here is only reported --
    # the tests launch their own instance and are the authority on whether the app runs.
    _dismiss_stray_windows(exe_path)
    _, probe_pid = _launch_app(exe_path, launch_ps1=launch_ps1 or None)
    if probe_pid is None:
        print("    WARNING: app failed to launch for the post-launch screenshot")
    _capture_screenshot(results_dir, "postlaunch_desktop1.png")
    _kill_app(exe_path, pid=probe_pid)

    # Run each test module — conftest handles per-test app launch/kill
    module_errors = 0
    for test_file in test_files:
        module = _module_name(test_file)
        print(f"  === {module} ===")

        # Dismiss stray system dialogs before the module runs
        _dismiss_stray_windows(exe_path)

        # Capture a pre-module screenshot for debugging
        _capture_screenshot(results_dir, f"pre_{module}.png")

        # Run tests — conftest launches fresh app per test, kills after
        result = _run_test_module(
            test_file,
            exe_path,
            results_dir,
            eval_fixtures,
            launch_ps1=launch_ps1 or "",
        )
        if result.get("module_error"):
            module_errors += 1

        # Ensure result file exists
        result_path = Path(results_dir) / f"{module}_results.json"
        if not result_path.exists():
            variant_exists = any(
                _is_module_match(c.stem.removesuffix("_results"), module)
                for c in Path(results_dir).glob("*_results.json")
                if c.name
                not in (
                    "programmatic_results.json",
                    "vlm_results.json",
                    "vlm_results.partial.json",
                )
            )
            if not variant_exists:
                result_path.write_text(json.dumps(result, indent=2), encoding="utf-8")

        # Kill any leftover processes between modules (single-instance apps)
        _kill_app(exe_path)

    # Collect summary
    summary = _collect_summary(results_dir, test_files)
    summary["total_modules"] = test_count
    summary["module_errors"] = module_errors
    summary_path = Path(results_dir) / "programmatic_results.json"
    summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    print(
        f"\n  Programmatic: {summary['passed']}/{summary['total']} ({summary['pass_rate']:.1%})"
    )
    if module_errors:
        print(f"  Module errors: {module_errors}/{test_count}")
    ss_count = len(list(Path(results_dir).glob("screenshots/*.png")))
    print(f"  Screenshots: {ss_count}")

    return summary


def run_vlm_phase(
    task_id: str,
    variant: str = "recreation",
    vlm_key: str = "",
    results_dir: str = "",
    vlm_model: str = "",
    vlm_base_url: str = "",
) -> dict:
    """Phase 2: delegate screenshot assertions to the shared desktop judge."""
    if not results_dir:
        base = vm_task_dir(task_id)
        results_dir = os.path.join(base, f"uia_eval_{variant}")
    ss_dir = os.path.join(results_dir, "screenshots")
    summary = shared_vlm_judge.evaluate_dir(
        ss_dir,
        model=vlm_model,
        base_url=vlm_base_url,
        api_key=vlm_key,
        missing_as_fail=True,
        max_error_rate=float(os.environ.get("VLM_MAX_JUDGE_ERROR_RATE", "0.10")),
        partial_output=os.path.join(results_dir, "vlm_results.partial.json"),
    )
    if summary is None:
        print("  No assertions found (neither jsonl nor json), skipping VLM eval")
        summary = {
            "passed": 0,
            "total": 0,
            "pass_rate": 0,
            "results": {},
            "skipped": "no_assertions",
        }
    Path(os.path.join(results_dir, "vlm_results.json")).write_text(
        json.dumps(summary, indent=2), encoding="utf-8"
    )
    _merge_results(results_dir)
    return summary


def _merge_results(results_dir: str) -> dict:
    """Merge programmatic and VLM results into final uia_eval.json."""
    prog = {}
    vlm = {}
    try:
        prog = json.loads(
            Path(os.path.join(results_dir, "programmatic_results.json")).read_text(
                encoding="utf-8-sig"
            )
        )
    except (OSError, json.JSONDecodeError):
        pass
    # Read vlm_results.json, fallback to vlm_results.partial.json if full results
    # don't exist (VLM judge may have timed out mid-run)
    for vlm_fname in ("vlm_results.json", "vlm_results.partial.json"):
        try:
            vlm = json.loads(
                Path(os.path.join(results_dir, vlm_fname)).read_text(
                    encoding="utf-8-sig"
                )
            )
            if vlm:
                break
        except (OSError, json.JSONDecodeError):
            pass

    combined = {
        "programmatic": {
            "passed": prog.get("passed", 0),
            "total": prog.get("total", 0),
            "pass_rate": prog.get("pass_rate", 0),
            "total_modules": prog.get("total_modules", 0),
            "module_errors": prog.get("module_errors", 0),
            "by_module": prog.get("by_module", {}),
        },
        "visual": {
            "passed": vlm.get("passed", 0),
            "total": vlm.get("total", 0),
            "pass_rate": vlm.get("pass_rate", 0),
            "judge_errors": vlm.get("judge_errors", 0),
            "judge_error_rate": vlm.get("judge_error_rate", 0),
            "missing_screenshots": vlm.get("missing_screenshots", 0),
            "skipped": vlm.get("skipped", ""),
            "warning": vlm.get("warning", ""),
            "error": vlm.get("error", ""),
        },
    }
    Path(os.path.join(results_dir, "uia_eval.json")).write_text(
        json.dumps(combined, indent=2), encoding="utf-8"
    )
    return combined


def run(
    task_id: str,
    exe_path: str,
    variant: str = "recreation",
    tests_dir: str | None = None,
    fixtures_dir: str | None = None,
    vlm_key: str = "",
    vlm_model: str = "",
    vlm_base_url: str = "",
    launch_ps1: str | None = None,
    manifest: str = "",
) -> dict:
    """Run full UIA eval (Phase 1 + Phase 2).

    Args:
        task_id: Task identifier
        exe_path: Path to executable to evaluate
        variant: recreation variant name
        tests_dir: Directory containing the frozen test suite
        fixtures_dir: Directory containing test fixtures
        vlm_key: API key for VLM judge
        launch_ps1: Path to launch.ps1 script (preferred over direct exe launch)
        manifest: If set, score against this manifest (fixed denominator for prog + VLM)

    Returns:
        Combined eval results dict
    """
    print("\n  --- PHASE 1: UIA Tests ---")
    phase1 = run_phase1(
        task_id=task_id,
        exe_path=exe_path,
        variant=variant,
        tests_dir=tests_dir,
        fixtures_dir=fixtures_dir,
        launch_ps1=launch_ps1,
    )

    base = vm_task_dir(task_id)
    results_dir = os.path.join(base, f"uia_eval_{variant}")

    if phase1.get("error"):
        _merge_results(results_dir)
        return phase1

    # Score programmatic against manifest (fixed denominator)
    manifest_data = None
    if manifest and os.path.exists(manifest):
        try:
            manifest_data = json.loads(Path(manifest).read_text(encoding="utf-8-sig"))
        except (OSError, json.JSONDecodeError):
            print(f"  WARNING: could not read manifest {manifest}")
    if manifest_data:
        scored = score_against_manifest(results_dir, manifest_data)
        prog_path = Path(results_dir) / "programmatic_results.json"
        prog_path.write_text(json.dumps(scored, indent=2), encoding="utf-8")
        print(
            f"  Manifest scored (prog): {scored['passed']}/{scored['total']} ({scored['pass_rate']:.1%})"
        )

    print("\n  --- PHASE 2: VLM Judge ---")
    run_vlm_phase(
        task_id=task_id,
        variant=variant,
        vlm_key=vlm_key,
        vlm_model=vlm_model,
        vlm_base_url=vlm_base_url,
    )

    # Score VLM against manifest (fixed denominator)
    if manifest_data:
        vlm_scored = score_vlm_against_manifest(results_dir, manifest_data)
        if vlm_scored:
            vlm_path = Path(results_dir) / "vlm_results.json"
            vlm_path.write_text(json.dumps(vlm_scored, indent=2), encoding="utf-8")
            print(
                f"  Manifest scored (vlm): {vlm_scored['passed']}/{vlm_scored['total']} ({vlm_scored['pass_rate']:.1%})"
            )

    return _merge_results(results_dir)


# ---------------------------------------------------------------------------
# Standalone frozen-suite evaluator
# ---------------------------------------------------------------------------


def _cli_main() -> int:
    """Standalone CLI for running Phase 1 (test execution) + optional Phase 2 (VLM).

    Usage:
        python uia_eval.py --binary C:\\path\\to\\app.exe --tests C:\\tests --results C:\\results
        python uia_eval.py --binary ... --results ... --vlm-key sk-xxx --vlm-model m --vlm-base-url u
    """
    import argparse as _ap

    parser = _ap.ArgumentParser(description="UIA Test Runner")
    parser.add_argument("--binary", required=True, help="Path to executable")
    parser.add_argument(
        "--tests", required=True, help="Directory with test_uia_*.py files"
    )
    parser.add_argument("--results", required=True, help="Results output directory")
    parser.add_argument("--fixtures", default="", help="Fixtures directory")
    parser.add_argument(
        "--launch-script",
        default="",
        help="Path to launch.ps1 (preferred over direct exe launch)",
    )
    parser.add_argument(
        "--summary", default="programmatic_results.json", help="Summary filename"
    )
    parser.add_argument(
        "--fail-under",
        type=float,
        default=None,
        help="Minimum pass rate (exit 1 if below)",
    )
    parser.add_argument(
        "--print-summary", action="store_true", help="Print summary JSON to stdout"
    )
    parser.add_argument(
        "--vlm-key",
        default="",
        help="VLM API key — when provided, run VLM eval after programmatic eval",
    )
    parser.add_argument("--vlm-model", default="", help="VLM model name")
    parser.add_argument("--vlm-base-url", default="", help="VLM API base URL")
    parser.add_argument(
        "--manifest",
        required=True,
        help="Frozen manifest used as the fixed denominator",
    )
    args = parser.parse_args()

    fixtures = args.fixtures or os.path.join(args.tests, "fixtures")
    launch_script = args.launch_script if args.launch_script else None

    summary = run_phase1(
        exe_path=args.binary,
        tests_dir=args.tests,
        results_dir=args.results,
        fixtures_dir=fixtures,
        launch_ps1=launch_script,
    )

    # --- Frozen-manifest scoring (programmatic) ---
    manifest_data = None
    if args.manifest and os.path.exists(args.manifest):
        try:
            manifest_data = json.loads(
                Path(args.manifest).read_text(encoding="utf-8-sig")
            )
        except (OSError, json.JSONDecodeError):
            print(f"  WARNING: could not read manifest {args.manifest}")
    if manifest_data:
        scored = score_against_manifest(args.results, manifest_data)
        Path(os.path.join(args.results, "programmatic_results.json")).write_text(
            json.dumps(scored, indent=2), encoding="utf-8"
        )
        summary = scored
        print(
            f"  Manifest scored (prog): {scored['passed']}/{scored['total']} ({scored['pass_rate']:.1%})"
        )

    # Write summary with custom name if specified
    if args.summary != "programmatic_results.json":
        summary_path = Path(args.results) / args.summary
        summary_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")

    if args.print_summary:
        print(json.dumps(summary))

    # --- VLM eval (Phase 2) ---
    vlm_key = (
        args.vlm_key
        or os.environ.get("VLM_API_KEY", "")
        or os.environ.get("VLM_MODEL_API_KEY", "")
    )
    if vlm_key and args.vlm_model and args.vlm_base_url:
        vlm_result = run_vlm_phase(
            task_id="",
            vlm_key=vlm_key,
            results_dir=args.results,
            vlm_model=args.vlm_model,
            vlm_base_url=args.vlm_base_url,
        )
        # Manifest scoring (VLM)
        if manifest_data:
            vlm_scored = score_vlm_against_manifest(args.results, manifest_data)
            if vlm_scored:
                Path(os.path.join(args.results, "vlm_results.json")).write_text(
                    json.dumps(vlm_scored, indent=2), encoding="utf-8"
                )
                vlm_result = vlm_scored
                print(
                    f"  Manifest scored (vlm): {vlm_scored['passed']}/{vlm_scored['total']} ({vlm_scored['pass_rate']:.1%})"
                )
        vp = vlm_result.get("passed", 0)
        vt = vlm_result.get("total", 0)
        vr = vlm_result.get("pass_rate", 0)
        print(f"VLM: {vp}/{vt} ({vr:.1%})")
        if args.print_summary:
            print(json.dumps(vlm_result))

    if summary.get("error"):
        return 2
    if args.fail_under is not None and summary.get("pass_rate", 0) < args.fail_under:
        print(
            f"FAIL: pass rate {summary['pass_rate']:.1%} < {args.fail_under:.1%}",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_cli_main())
