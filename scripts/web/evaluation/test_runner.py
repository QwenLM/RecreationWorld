"""Playwright evaluator runner for RecreationBench Web.

Runs four-dimensional Playwright tests against agent output,
parsing results by dimension (functional/visual/structural/quality).
"""

import asyncio
import json
import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from core import runtime_assets

logger = logging.getLogger(__name__)


@dataclass
class TestDimensionResult:
    """Result for one test dimension."""
    dimension: str
    pass_rate: float
    total: int
    passed: int
    failed: int
    skipped: int = 0
    compile_error: bool = False
    details: list[dict] = field(default_factory=list)


PLAYWRIGHT_CONFIG_TEMPLATE = runtime_assets.load_text("web/runtime_assets/playwright.config.ts.template")

DIMENSIONS = ["functional", "visual", "structural", "quality"]


class EvaluationSetupError(RuntimeError):
    """The evaluator is incomplete, so producing a benchmark score is invalid."""


def _find_node_modules() -> Path | None:
    """Locate the Node install that provides @playwright/test.

    Checks, in order: an explicit MOCKWEB_NODE_MODULES override; a node_modules
    next to this code (self-built image or setup_eval_env.sh); and
    batch_run/node_modules, where the shared Web evaluator image bakes the
    Node dependencies. The last candidate lets the functional/structural test
    dimensions run on that image with no separate npm install.
    """
    from config import PROJECT_ROOT

    configured = os.environ.get("MOCKWEB_NODE_MODULES")
    candidates = [
        Path(configured).expanduser() if configured else None,
        PROJECT_ROOT / "node_modules",
        PROJECT_ROOT / "batch_run" / "node_modules",
    ]
    for candidate in candidates:
        if candidate and (candidate / "@playwright" / "test").is_dir():
            return candidate.resolve()
    return None


def _maybe_copy_kit(tmpdir: Path, test_files: list[Path]) -> None:
    """Place the runtime kit beside the flattened specs so ``./rb_web_kit`` resolves.

    The flatten step copies ONLY ``*.spec.ts`` into tmpdir, so a kit module that the
    dataset ships as a SIBLING of the specs (the release carries it as
    ``evaluation/tests/{,agent_gen/}rb_web_kit.ts``) is left behind and every spec in
    that Playwright group dies with "Cannot find module './rb_web_kit'". That failure
    is silent: the message matches no marker in HARD_COMPILE_MARKERS, so
    ``compile_error`` is never set and the group just reports zero tests — which
    INFLATES the functional score, because the interaction specs that need the kit
    pass at a much lower rate than the scripted ones they are averaged with.

    Sourced from each spec's own parent dir (not a vendored template path): the kit
    travels with the dataset, and specs from scripted/, agent_gen/ and the tests root
    are flattened into one tmpdir, so the first sibling copy found serves them all.
    """
    specs = list(tmpdir.glob("*.spec.ts"))
    if not any("rb_web_kit" in s.read_text(encoding="utf-8") for s in specs):
        return
    searched = []
    for spec_dir in dict.fromkeys(f.parent for f in test_files):   # de-dup, order-stable
        searched.append(spec_dir)
        src = spec_dir / "rb_web_kit.ts"
        if src.is_file():
            shutil.copy(str(src), str(tmpdir / "rb_web_kit.ts"))
            return
    raise EvaluationSetupError(
        "specs import './rb_web_kit' but no rb_web_kit.ts sits beside them "
        f"(looked in {[str(d) for d in searched]}); refusing to emit a score "
        "from an incomplete functional test lane"
    )


def _discover_test_files(test_dir: Path) -> dict[str, list[Path]]:
    """Discover test files organized by dimension.

    Looks in both scripted/ and agent_gen/ subdirectories.
    Maps dimension names to their test files.
    """
    files_by_dim: dict[str, list[Path]] = {d: [] for d in DIMENSIONS}

    for sub in ["scripted", "agent_gen", "."]:
        search_dir = test_dir / sub if sub != "." else test_dir
        if not search_dir.is_dir():
            continue
        for f in sorted(search_dir.glob("*.spec.ts")):
            for dim in DIMENSIONS:
                if dim in f.stem:
                    files_by_dim[dim].append(f)
                    break

    return files_by_dim


async def _run_playwright_tests(
    test_files: list[Path],
    base_url: str,
    timeout: int = 3600,
) -> dict:
    """Run Playwright tests and return parsed results."""
    if not test_files:
        return {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "details": []}

    with tempfile.TemporaryDirectory() as tmpdir:
        tmpdir = Path(tmpdir)
        results_file = tmpdir / "test-results.json"

        # Copy test files to temp dir, fixing hardcoded base URLs
        # Use unique names to avoid overwrites when scripted/ and agent_gen/ have same filenames
        _HARDCODED_URLS = ["http://localhost:8100", "http://localhost:8101"]
        for f in test_files:
            content = f.read_text(encoding="utf-8")
            for hurl in _HARDCODED_URLS:
                content = content.replace(hurl, base_url.rstrip("/"))
            parent_name = f.parent.name if f.parent.name in ("scripted", "agent_gen") else "root"
            dest_name = f"{parent_name}__{f.name}"
            (tmpdir / dest_name).write_text(content, encoding="utf-8")

        # Copy the runtime kit alongside the specs when any references it, so the
        # relative import ('./rb_web_kit') resolves in the flattened tmpdir.
        _maybe_copy_kit(tmpdir, test_files)

        # Symlink node_modules so the generated config can import @playwright/test.
        nm_source = _find_node_modules()
        if nm_source is None:
            raise EvaluationSetupError(
                "@playwright/test not found; run setup_eval_env.sh or set "
                "MOCKWEB_NODE_MODULES; refusing to emit a score without the "
                "functional test runtime"
            )
        os.symlink(str(nm_source), str(tmpdir / "node_modules"))

        # Write Playwright config
        config_path = tmpdir / "playwright.config.ts"
        config_content = PLAYWRIGHT_CONFIG_TEMPLATE.format(
            test_dir=".",
            base_url=base_url,
            results_file=str(results_file),
        )
        config_path.write_text(config_content)

        # Run tests
        cmd = ["npx", "playwright", "test", "--config", "playwright.config.ts"]

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(tmpdir),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    process.communicate(),
                    timeout=timeout,
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                logger.warning("Playwright test timeout after %ds", timeout)
                return {"total": 0, "passed": 0, "failed": 0, "skipped": 0,
                        "details": [], "error": "timeout"}

        except FileNotFoundError:
            logger.error("npx/playwright not found")
            return {"total": 0, "passed": 0, "failed": 0, "skipped": 0,
                    "details": [], "error": "playwright_not_found"}

        # Parse results
        if results_file.exists():
            parsed = _parse_json_results(results_file)
        else:
            parsed = _parse_stdout_results(
                stdout.decode("utf-8", errors="replace") if stdout else ""
            )

        # Detect compile/transform errors. UNAMBIGUOUS markers are checked even
        # when total>0, because Playwright transforms each spec file independently
        # — a partial compile failure (one broken spec among runnable siblings)
        # must still surface compile_error instead of being masked.
        HARD_COMPILE_MARKERS = (
            "Transform failed", "SyntaxError", "could not be parsed",
            'can only be used inside an "async"', "has already been declared",
        )
        stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""
        if stderr_text and any(m in stderr_text for m in HARD_COMPILE_MARKERS):
            parsed["compile_error"] = True

        # When ZERO tests ran, broader markers (Expected/Unexpected) are also
        # compile-indicative (no assertions ran, so they aren't assertion noise).
        if parsed["total"] == 0 and stderr_text:
            file_names = [f.name for f in test_files]
            soft_markers = HARD_COMPILE_MARKERS + ("Expected ", "Unexpected ")
            if parsed.get("compile_error") or any(m in stderr_text for m in soft_markers):
                parsed["compile_error"] = True
                logger.error(
                    "COMPILE ERROR in test files %s — these contribute 0 tests "
                    "(NOT a benign empty result). stderr (last 800 chars): %s",
                    file_names, stderr_text[-800:],
                )
            else:
                logger.warning(
                    "Playwright returned 0 tests for files %s. stderr (last 500 chars): %s",
                    file_names, stderr_text[-500:],
                )

        return parsed


def _parse_json_results(results_file: Path) -> dict:
    """Parse Playwright JSON reporter output."""
    try:
        data = json.loads(results_file.read_text())
    except (json.JSONDecodeError, FileNotFoundError):
        return {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "details": []}

    total = 0
    passed = 0
    failed = 0
    skipped = 0
    details = []

    def _collect_from_suite(suite, cur_file=""):
        nonlocal total, passed, failed, skipped
        # Track the originating spec file so downstream can attribute each test to
        # its source (a suite carries "file"; nested describe-suites inherit it).
        # Purely additive: nothing in scoring reads "file" — it feeds the
        # functional_subscores / test_details.json diagnostics.
        suite_file = suite.get("file") or cur_file
        for spec in suite.get("specs", []):
            spec_file = spec.get("file") or suite_file
            for test in spec.get("tests", []):
                total += 1
                expected = test.get("expectedStatus", "passed")
                results = test.get("results", [])
                actual = results[0].get("status", "failed") if results else "failed"
                if actual == "skipped":
                    skipped += 1
                elif actual == expected:
                    passed += 1
                else:
                    failed += 1
                details.append({
                    "title": spec.get("title", ""),
                    "status": actual,
                    "file": os.path.basename(spec_file) if spec_file else "",
                })
        for sub in suite.get("suites", []):
            _collect_from_suite(sub, suite_file)

    for suite in data.get("suites", []):
        _collect_from_suite(suite)

    return {"total": total, "passed": passed, "failed": failed,
            "skipped": skipped, "details": details}


def _parse_stdout_results(stdout: str) -> dict:
    """Parse Playwright stdout when JSON reporter fails."""
    import re

    passed = 0
    failed = 0
    total = 0

    match = re.search(r"(\d+) passed", stdout)
    if match:
        passed = int(match.group(1))
    match = re.search(r"(\d+) failed", stdout)
    if match:
        failed = int(match.group(1))

    total = passed + failed
    return {"total": total, "passed": passed, "failed": failed,
            "skipped": 0, "details": []}


async def run_tests(
    test_dir: Path,
    agent_url: str,
    eval_config: dict = None,
    skip_dimensions: list[str] = None,
) -> dict[str, TestDimensionResult]:
    """Run all four-dimensional tests and return results by dimension.

    Runs scripted and agent_gen test files in separate Playwright processes
    so that a compilation error in one source does not kill the other.

    Args:
        test_dir: Directory containing test files (with scripted/ and agent_gen/ subdirs)
        agent_url: URL of the served agent output
        eval_config: Reserved/unused. Source weighting was removed (scripted +
            agent_gen are pooled by case count); kept in the signature for
            backward compatibility with existing callers.
        skip_dimensions: List of dimension names to skip (e.g. ["visual"])

    Returns:
        dict mapping dimension name to TestDimensionResult
    """
    files_by_dim = _discover_test_files(test_dir)
    results = {}

    for dim in DIMENSIONS:
        if skip_dimensions and dim in skip_dimensions:
            continue

        test_files = files_by_dim.get(dim, [])

        if not test_files:
            results[dim] = TestDimensionResult(
                dimension=dim, pass_rate=0.0,
                total=0, passed=0, failed=0,
            )
            continue

        scripted_files = [f for f in test_files if "scripted" in str(f.parent)]
        agent_files = [f for f in test_files if "agent_gen" in str(f.parent)]
        other_files = [f for f in test_files
                      if f not in scripted_files and f not in agent_files]

        groups = []
        if scripted_files:
            groups.append(("scripted", scripted_files))
        if agent_files:
            groups.append(("agent_gen", agent_files))
        if other_files:
            groups.append(("other", other_files))

        total = 0
        passed_count = 0
        failed_count = 0
        skipped_count = 0
        all_details = []
        compile_error_flag = False

        for source_name, source_files in groups:
            raw = await _run_playwright_tests(source_files, agent_url)
            src_total = raw["total"]
            src_passed = raw["passed"]
            total += src_total
            passed_count += src_passed
            failed_count += raw["failed"]
            skipped_count += raw.get("skipped", 0)
            all_details.extend(raw.get("details", []))
            if raw.get("compile_error"):
                compile_error_flag = True

            if src_total > 0:
                logger.info(
                    "Dimension %s [%s]: %d/%d passed (%.1f%%)",
                    dim, source_name, src_passed, src_total,
                    src_passed / src_total * 100,
                )
            elif raw.get("error"):
                logger.warning(
                    "Dimension %s [%s]: 0 tests found (error: %s)",
                    dim, source_name, raw["error"],
                )

        pass_rate = passed_count / total if total > 0 else 0.0

        results[dim] = TestDimensionResult(
            dimension=dim,
            pass_rate=pass_rate,
            total=total,
            passed=passed_count,
            failed=failed_count,
            skipped=skipped_count,
            compile_error=compile_error_flag,
            details=all_details,
        )

        logger.info(
            "Dimension %s: %d/%d passed (%.1f%%)",
            dim, passed_count, total, pass_rate * 100,
        )

    return results
