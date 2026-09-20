#!/usr/bin/env python3
"""Run generated AT-SPI tests with consistent GUI state isolation.

This script is intentionally self-contained because it is uploaded into the
VM/Docker workspace and used by test generation, pipeline verification, and
evaluation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PASS_STATUSES = {"PASS", "PASSED", "OK", "SUCCESS"}
SKIP_PREFIXES = ("programmatic", "vlm", "atspi_eval", "baseline_results")


def result_files(results_dir: Path) -> list[Path]:
    files: list[Path] = []
    for pattern in (
        "*_results.json",
        "*_eval.json",
        "results/*_results.json",
        "results/*_eval.json",
    ):
        files.extend(results_dir.glob(pattern))
    return sorted(
        {
            path
            for path in files
            if path.is_file() and not path.name.startswith(SKIP_PREFIXES)
        }
    )


def is_pass(item: object) -> bool:
    if not isinstance(item, dict):
        return False
    if isinstance(item.get("passed"), bool):
        return bool(item["passed"])
    status = str(item.get("status", "")).upper()
    return status in PASS_STATUSES


def summarize_result(data: dict) -> tuple[int, int]:
    tests = data.get("tests")
    if isinstance(tests, list):
        return sum(1 for item in tests if is_pass(item)), len(tests)
    if isinstance(tests, dict):
        values = list(tests.values())
        return sum(1 for item in values if is_pass(item)), len(values)
    if isinstance(data.get("summary"), dict):
        summary = data["summary"]
        return int(summary.get("passed", 0)), int(summary.get("total", 0))
    if "passed" in data and "total" in data:
        return int(data.get("passed", 0)), int(data.get("total", 0))
    if "total_tests" in data:
        return int(data.get("passed", 0)), int(data.get("total_tests", 0))
    return 0, 0


def module_name(path: Path) -> str:
    name = path.name
    for suffix in ("_results.json", "_eval.json"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    return name.removeprefix("test_atspi_").removeprefix("atspi_")


def collect_summary(results_dir: Path, test_files: list[Path], display: str) -> dict:
    by_module: dict[str, dict[str, int]] = {}
    total_pass = 0
    total_tests = 0
    parsed_files: list[str] = []

    for path in result_files(results_dir):
        try:
            data = json.loads(path.read_text())
            passed, total = summarize_result(data)
        except Exception:
            continue
        if total <= 0:
            continue
        key = module_name(path)
        by_module[key] = {"passed": passed, "total": total}
        total_pass += passed
        total_tests += total
        parsed_files.append(str(path))

    return {
        "passed": total_pass,
        "total": total_tests,
        "pass_rate": round(total_pass / total_tests, 4) if total_tests else 0,
        "by_module": by_module,
        "runner": {
            "test_files": len(test_files),
            "result_files": len(parsed_files),
            "display": display,
            "isolation": "module",
        },
    }


# ── Canonical status + frozen-manifest scoring ──────────────────────────────
# These functions give every candidate the SAME denominator for a task: the
# canonical (passing) test set is frozen from the baseline run into a manifest,
# and each candidate is scored against it. A test missing from a candidate's
# output (crash, early-return, missing feature) is injected as "not_run" and
# counts as a failure against the fixed denominator instead of shrinking it.
CANONICAL_STATUSES = {"passed", "failed", "error", "skipped", "not_run"}


def record_status(item: object) -> str:
    """Map a heterogeneous per-test record to a canonical status string."""
    if not isinstance(item, dict):
        return "failed"
    raw = item.get("status")
    if isinstance(raw, str) and raw:
        s = raw.strip().lower()
        if s in {"pass", "passed", "ok", "success"}:
            return "passed"
        if s in {"skip", "skipped"}:
            return "skipped"
        if s in {"not_run", "notrun", "missing"}:
            return "not_run"
        if s in {"error", "system_error"}:
            return "error"
        return "failed"
    passed = item.get("passed")
    if isinstance(passed, bool):
        return "passed" if passed else "failed"
    return "failed"


def qualify_name(module: str, name: str) -> str:
    """Return a globally-unique ``module::test`` name (idempotent)."""
    name = str(name)
    return name if "::" in name else f"{module}::{name}"


def iter_result_statuses(results_dir: Path) -> dict[str, str]:
    """Collect ``{module::test: status}`` from every per-module result file.

    On duplicate names a ``passed`` wins over a non-pass; otherwise later
    files overwrite earlier ones.
    """
    statuses: dict[str, str] = {}
    for path in result_files(results_dir):
        try:
            data = json.loads(path.read_text())
        except Exception:
            continue
        module = data.get("module") or module_name(path)
        records = data.get("tests")
        if isinstance(records, dict):
            records = list(records.values())
        if not isinstance(records, list):
            continue
        for rec in records:
            if not isinstance(rec, dict):
                continue
            raw_name = rec.get("name")
            if not raw_name:
                continue
            qn = qualify_name(module, raw_name)
            status = record_status(rec)
            if statuses.get(qn) == "passed":
                continue
            statuses[qn] = status
    return statuses


def read_vlm_assertions(results_dir: Path) -> dict[str, str]:
    """Complete ``{screenshot_name: description}`` set for VLM judging.

    The append-only ``screenshot_asserts.jsonl`` provides the complete KEY set
    (it survives the read-modify-write clobbering that truncates
    ``assertions.json`` across per-module test processes); ``assertions.json``
    provides the canonical description text. Keys are unioned; the description
    prefers ``assertions.json`` and falls back to the jsonl record.
    """
    results_dir = Path(results_dir)
    jsonl_desc: dict[str, str] = {}
    for jsonl in (
        results_dir / "screenshots" / "screenshot_asserts.jsonl",
        results_dir / "screenshot_asserts.jsonl",
    ):
        if not jsonl.exists():
            continue
        for line in jsonl.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            key = rec.get("screenshot") or rec.get("name") or rec.get("file")
            if not key:
                continue
            desc = rec.get("description") or rec.get("assertion") or rec.get("desc")
            jsonl_desc[str(key)] = str(desc or "")
    dict_desc: dict[str, str] = {}
    for cand in (
        results_dir / "screenshots" / "assertions.json",
        results_dir / "assertions.json",
    ):
        if not cand.exists():
            continue
        try:
            data = json.loads(cand.read_text())
        except Exception:
            continue
        if isinstance(data, dict):
            for key, val in data.items():
                dict_desc.setdefault(str(key), str(val))
    out: dict[str, str] = {}
    for key in set(jsonl_desc) | set(dict_desc):
        out[key] = dict_desc.get(key) or jsonl_desc.get(key, "")
    return dict(sorted(out.items()))


def read_vlm_assertion_names(results_dir: Path) -> list[str]:
    """Complete sorted set of VLM assertion keys (see read_vlm_assertions)."""
    return list(read_vlm_assertions(results_dir).keys())


def build_manifest(
    baseline_results_dir: Path,
    *,
    task_id: str = "",
    baseline_model: str = "",
    baseline_commit: str = "",
) -> dict:
    """Freeze the canonical (baseline-passing) test set into a manifest dict.

    Only tests that PASS on the original build enter ``tests`` (the fixed
    denominator); baseline-failing tests are recorded in ``ignored`` with
    reason ``baseline_fail`` (the ProgramBench ``gold_fail`` analogue).
    """
    baseline_results_dir = Path(baseline_results_dir)
    statuses = iter_result_statuses(baseline_results_dir)
    tests: list[dict] = []
    ignored: list[dict] = []
    for qn in sorted(statuses):
        if qn.endswith("_runner"):
            continue  # harness failure marker, not a real test
        module = qn.split("::", 1)[0]
        if statuses[qn] == "passed":
            tests.append({"name": qn, "module": module, "baseline_status": "passed"})
        else:
            ignored.append({"name": qn, "module": module, "reason": "baseline_fail"})
    sha = hashlib.sha256("\n".join(t["name"] for t in tests).encode()).hexdigest()[:12]
    vlm_map = read_vlm_assertions(baseline_results_dir)
    vlm = [{"name": k, "description": vlm_map[k]} for k in sorted(vlm_map)]
    return {
        "manifest_version": 1,
        "task_id": task_id,
        "baseline_model": baseline_model,
        "baseline_commit": baseline_commit,
        "total": len(tests),
        "manifest_sha": sha,
        "tests": tests,
        "ignored": ignored,
        "vlm_assertions": vlm,
        "vlm_total": len(vlm),
    }


def score_against_manifest(
    results_dir: Path, manifest: dict, display: str = ""
) -> dict:
    """Score a candidate run against a frozen manifest (denominator fixed).

    Every manifest test absent from the candidate's results is injected as
    ``not_run`` (a non-pass) so the denominator equals the manifest size for
    every candidate. Candidate tests not in the manifest are reported as
    ``unexpected`` and excluded from the score.
    """
    results_dir = Path(results_dir)
    statuses = iter_result_statuses(results_dir)
    expected = manifest.get("tests", []) or []
    expected_names = {t["name"] for t in expected}
    ignored_names = {i.get("name") for i in (manifest.get("ignored") or [])}

    per_test: dict[str, str] = {}
    by_module: dict[str, dict[str, int]] = {}
    passed = 0
    injected = 0
    for t in expected:
        qn = t["name"]
        module = t.get("module") or qn.split("::", 1)[0]
        status = statuses.get(qn)
        if status is None:
            status = "not_run"
            injected += 1
        elif status not in CANONICAL_STATUSES:
            status = "failed"
        per_test[qn] = status
        bucket = by_module.setdefault(module, {"passed": 0, "total": 0})
        bucket["total"] += 1
        if status == "passed":
            passed += 1
            bucket["passed"] += 1

    total = len(expected)
    unexpected = sorted(
        n
        for n in statuses
        if n not in expected_names
        and n not in ignored_names
        and not n.endswith("_runner")
    )
    summary = {
        "passed": passed,
        "total": total,
        "pass_rate": round(passed / total, 4) if total else 0.0,
        "by_module": by_module,
        "scored_against": "manifest",
        "manifest_sha": manifest.get("manifest_sha", ""),
        "manifest_total": manifest.get("total", total),
        "n_injected_not_run": injected,
        "n_not_run": sum(1 for status in per_test.values() if status == "not_run"),
        "n_unexpected": len(unexpected),
        "unexpected": unexpected[:50],
        "per_test": per_test,
        "runner": {"display": display, "isolation": "module"},
    }
    summary["n_executed"] = total - summary["n_not_run"]
    if total == 0:
        summary["error"] = "empty_manifest"
    elif summary["n_executed"] == 0:
        # A process that cannot start is a property of the submitted candidate,
        # not an evaluator outage.  Preserve that distinction so the outer
        # pipeline records a terminal zero instead of retrying the agent.  Keep
        # generic all-not-run outcomes fail-closed because display/test-runner
        # failures can produce the same counts without this explicit evidence.
        runner_errors = []
        app_launch_failure_modules = set()
        for path in result_files(results_dir):
            try:
                records = json.loads(path.read_text()).get("tests")
            except Exception:
                continue
            if isinstance(records, dict):
                records = list(records.values())
            if not isinstance(records, list):
                continue
            for record in records:
                if not isinstance(record, dict):
                    continue
                if not str(record.get("name") or "").endswith("_runner"):
                    continue
                error = str(record.get("error") or record.get("message") or "").strip()
                if error:
                    runner_errors.append(error)
                    if error == "app failed to start":
                        app_launch_failure_modules.add(
                            str(record.get("name"))[: -len("_runner")]
                        )
        expected_modules = {
            str(item.get("module") or str(item.get("name") or "").split("::", 1)[0])
            for item in expected
            if isinstance(item, dict)
        }
        if (
            expected_modules
            and expected_modules.issubset(app_launch_failure_modules)
            and all(error == "app failed to start" for error in runner_errors)
        ):
            summary["error"] = "app_launch_failed"
        else:
            summary["error"] = "no_tests_executed"
        if runner_errors:
            summary["runner"]["failure_reasons"] = sorted(set(runner_errors))
    return summary


def select_vlm_assertions(
    results_dir: Path, manifest: dict
) -> tuple[dict[str, str], list[str]]:
    """Split a candidate's VLM work against the frozen manifest assertions.

    Returns ``(to_judge, missing)`` where ``to_judge`` maps each manifest
    assertion whose screenshot the candidate actually produced to its frozen
    description, and ``missing`` lists manifest assertions with no candidate
    screenshot. Missing assertions are injected as failures by the caller so
    the VLM denominator equals the manifest size for every candidate.
    """
    results_dir = Path(results_dir)
    ss_dir = results_dir / "screenshots"
    expected = manifest.get("vlm_assertions") or []
    to_judge: dict[str, str] = {}
    missing: list[str] = []
    for assertion in expected:
        name = assertion.get("name") if isinstance(assertion, dict) else None
        if not name:
            continue
        if (ss_dir / name).exists():
            to_judge[name] = assertion.get("description", "")
        else:
            missing.append(name)
    return to_judge, missing


def find_tests(tests_dir: Path) -> list[Path]:
    tests = list(tests_dir.glob("test_atspi_*.py"))
    tests.extend((tests_dir / "tests").glob("test_atspi_*.py"))
    return sorted({path.resolve() for path in tests if path.is_file()})


def clean_results(results_dir: Path, summary_name: str) -> None:
    results_dir.mkdir(parents=True, exist_ok=True)
    for path in result_files(results_dir):
        path.unlink(missing_ok=True)
    for name in ("programmatic_results.json", "atspi_eval.json", summary_name):
        (results_dir / name).unlink(missing_ok=True)
    shutil.rmtree(results_dir / "screenshots", ignore_errors=True)
    shutil.rmtree(results_dir / "results", ignore_errors=True)
    (results_dir / "results").mkdir(parents=True, exist_ok=True)


def write_runner_failure(results_dir: Path, module: str, reason: str) -> None:
    path = results_dir / f"{module}_runner_results.json"
    path.write_text(
        json.dumps(
            {"tests": [{"name": f"{module}_runner", "passed": False, "error": reason}]},
            indent=2,
        )
    )


def terminate_process(proc: subprocess.Popen | None, timeout: float = 2.0) -> None:
    if proc is None or proc.poll() is not None:
        return
    try:
        proc.terminate()
        proc.wait(timeout=timeout)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def process_descendants(root: int) -> set[int]:
    """Return a process and the descendants visible through pgrep."""

    descendants = {root}
    frontier = [root]
    while frontier:
        parent = frontier.pop()
        try:
            children = subprocess.run(
                ["pgrep", "-P", str(parent)],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            ).stdout.split()
        except Exception:
            children = []
        for child_text in children:
            try:
                child = int(child_text)
            except ValueError:
                continue
            if child not in descendants:
                descendants.add(child)
                frontier.append(child)
    return descendants


def inspect_atspi_desktop() -> None:
    """Print applications and their first-level accessible children."""

    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    try:
        Atspi.init()
    except Exception as exc:
        print("Atspi.init failed:", exc)
    desktop = Atspi.get_desktop(0)
    child_count = desktop.get_child_count()
    print(f"ATSPI desktop children={child_count}")
    for index in range(child_count):
        try:
            app = desktop.get_child_at_index(index)
            print(
                f"  app name={app.get_name()!r} role={app.get_role_name()} "
                f"pid={app.get_process_id()} nchild={app.get_child_count()}"
            )
            for child_index in range(min(app.get_child_count(), 8)):
                child = app.get_child_at_index(child_index)
                print(
                    f"      child role={child.get_role_name()} "
                    f"name={child.get_name()!r}"
                )
        except Exception as exc:
            print("  [err]", exc)


def inspect_atspi_app_name(pid: int, fallback: str) -> str:
    """Resolve the accessible application belonging to a launched process tree."""

    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    time.sleep(1)
    pids = process_descendants(pid)
    desktop = Atspi.get_desktop(0)
    for index in range(desktop.get_child_count()):
        app = desktop.get_child_at_index(index)
        try:
            if app.get_process_id() in pids:
                return app.get_name() or fallback
        except Exception:
            pass
    return fallback


def display_number(display: str) -> str:
    return display.removeprefix(":").split(".", 1)[0]


def parse_dbus_env(output: str) -> dict[str, str]:
    env: dict[str, str] = {}
    for line in output.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key not in {"DBUS_SESSION_BUS_ADDRESS", "DBUS_SESSION_BUS_PID"}:
            continue
        value = value.split(";", 1)[0].strip().strip("'").strip('"')
        env[key] = value
    return env


class GuiSession:
    def __init__(
        self,
        *,
        display: str,
        screen: str,
        binary: str | None,
        launch_cmd: str | None,
        fallback_app_name: str,
        launch_wait: float,
        use_native: bool = False,
    ) -> None:
        self.display = display
        self.screen = screen
        self.binary = binary
        self.launch_cmd = launch_cmd
        self.fallback_app_name = fallback_app_name
        self.launch_wait = launch_wait
        self.use_native = use_native
        self.env = os.environ.copy()
        self.xvfb: subprocess.Popen | None = None
        self.atspi: subprocess.Popen | None = None
        self.openbox: subprocess.Popen | None = None
        self.app: subprocess.Popen | None = None
        self.dbus_pid: int | None = None
        # diagnostic: per-module file capturing the launched app's stdout+stderr,
        # so an a11y-bridge / crash / missing-arg failure is visible in the log
        # instead of vanishing into DEVNULL.
        self.app_diag: str | None = None

    def cleanup_stale_display(self) -> None:
        if self.use_native:
            return  # never pkill/unlock the shared native :0 session
        num = display_number(self.display)
        subprocess.run(
            ["pkill", "-f", f"Xvfb {self.display}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        Path(f"/tmp/.X{num}-lock").unlink(missing_ok=True)
        Path(f"/tmp/.X11-unix/X{num}").unlink(missing_ok=True)

    def reset_app_instance(self) -> None:
        """Reap a stale app process before launching the next isolated module."""
        if not self.binary:
            return
        self_pid = os.getpid()

        def stale_pids() -> list[int]:
            proc = subprocess.run(
                ["pgrep", "-f", "--", self.binary],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            return [
                int(token)
                for token in proc.stdout.split()
                if token.isdigit() and int(token) != self_pid
            ]

        def signal_pids(pids: list[int], sig: int) -> None:
            for pid in pids:
                try:
                    os.kill(pid, sig)
                except (ProcessLookupError, PermissionError):
                    pass

        signal_pids(stale_pids(), signal.SIGTERM)
        for _ in range(24):
            if not stale_pids():
                return
            time.sleep(0.25)
        signal_pids(stale_pids(), signal.SIGKILL)
        time.sleep(0.5)

    def start_stack(self) -> None:
        self.cleanup()
        self.cleanup_stale_display()
        self.env = os.environ.copy()
        a11y = {
            "GTK_MODULES": "gail:atk-bridge",
            "QT_LINUX_ACCESSIBILITY_ALWAYS_ON": "1",
            "QT_ACCESSIBILITY": "1",
            "ELECTRON_ENABLE_ACCESSIBILITY": "1",
            "GSK_RENDERER": os.environ.get("GSK_RENDERER", "cairo"),
            "LIBGL_ALWAYS_SOFTWARE": os.environ.get("LIBGL_ALWAYS_SOFTWARE", "1"),
        }
        if self.use_native:
            # Reuse the existing native desktop (e.g. GNOME :0) and its inherited
            # D-Bus / AT-SPI registry from the bootstrap env; only the app is
            # (re)launched per module. Do NOT start or tear down
            # Xvfb/dbus/at-spi/openbox — that would kill the host session.
            self.env.update(a11y)
            self.env["DISPLAY"] = self.display
            return
        self.env.update({"DISPLAY": self.display, **a11y})

        self.xvfb = subprocess.Popen(
            [
                "Xvfb",
                self.display,
                "-screen",
                "0",
                self.screen,
                "-ac",
                "+extension",
                "GLX",
                "+render",
                "-noreset",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=self.env,
        )
        time.sleep(1)

        dbus = subprocess.run(
            ["dbus-launch", "--sh-syntax"],
            check=True,
            capture_output=True,
            text=True,
            env=self.env,
        )
        self.env.update(parse_dbus_env(dbus.stdout))
        if self.env.get("DBUS_SESSION_BUS_PID", "").isdigit():
            self.dbus_pid = int(self.env["DBUS_SESSION_BUS_PID"])

        registry = next(
            (
                path
                for path in (
                    "/usr/libexec/at-spi2-registryd",
                    "/usr/lib/at-spi2-core/at-spi2-registryd",
                    shutil.which("at-spi2-registryd") or "",
                )
                if path and Path(path).exists()
            ),
            "",
        )
        if registry:
            self.atspi = subprocess.Popen(
                [registry],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                env=self.env,
            )

        self.openbox = subprocess.Popen(
            ["openbox"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env=self.env,
        )

        # This stack creates a fresh session bus, so bootstrap cannot have set
        # Chromium's accessibility gate on it.
        subprocess.run(
            [
                "gdbus",
                "call",
                "--session",
                "--dest",
                "org.a11y.Bus",
                "--object-path",
                "/org/a11y/bus",
                "--method",
                "org.freedesktop.DBus.Properties.Set",
                "org.a11y.Status",
                "ScreenReaderEnabled",
                "<boolean true>",
            ],
            env=self.env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        subprocess.run(
            ["pkill", "-f", "orca"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        time.sleep(1)

    def _app_out(self):
        if self.app_diag:
            try:
                return open(self.app_diag, "w")
            except Exception:
                return subprocess.DEVNULL
        return subprocess.DEVNULL

    def _electron_flags(self) -> str:
        """Return Chromium a11y argv only for an actual Electron install."""
        if not self.launch_cmd:
            return ""
        match = re.search(r"(/\S*?)/launch\.sh", self.launch_cmd)
        root = Path(match.group(1)) if match else None
        if root is None or not root.is_dir():
            return ""
        for candidate in (
            root / "runtime" / "electron",
            root / "node_modules" / "electron" / "dist" / "electron",
        ):
            try:
                if (
                    candidate.is_file()
                    and not candidate.is_symlink()
                    and os.access(candidate, os.X_OK)
                ):
                    with candidate.open("rb") as handle:
                        if handle.read(4) == b"\x7fELF":
                            return " --force-renderer-accessibility --no-zygote"
            except OSError:
                pass
        if (root / "resources" / "app").exists() or (
            root / "resources" / "app.asar"
        ).exists():
            return " --force-renderer-accessibility --no-zygote"
        return ""

    def _suite_launch_args(self, env: dict[str, str]) -> str:
        """Return the frozen suite's optional first-line launch arguments.

        Suites that need a document or directory open at process start place one
        argument fragment in ``<fixtures>/.rb_launch_args``. ``{FIXTURES}`` is
        expanded to the staged fixture directory. Missing, empty, and comment-only
        files leave the launch command unchanged.
        """
        fixtures = env.get("FIXTURES_DIR", "")
        if not fixtures:
            return ""
        path = Path(fixtures) / ".rb_launch_args"
        try:
            raw = path.read_text().strip()
        except OSError:
            return ""
        if not raw or raw.startswith("#"):
            return ""
        args = raw.splitlines()[0].strip().replace("{FIXTURES}", fixtures)
        return f" {args}" if args else ""

    @staticmethod
    def _restore_empty_fixture_dirs(env: dict[str, str]) -> None:
        """Recreate empty fixture directories declared by the frozen suite."""
        fixtures = env.get("FIXTURES_DIR", "")
        if not fixtures:
            return
        spec = Path(fixtures) / ".rb_mkdirs"
        try:
            lines = spec.read_text().splitlines()
        except OSError:
            return
        made = []
        for raw in lines:
            relative = raw.strip()
            if (
                not relative
                or relative.startswith("#")
                or relative.startswith("/")
                or ".." in Path(relative).parts
            ):
                continue
            try:
                (Path(fixtures) / relative).mkdir(parents=True, exist_ok=True)
                made.append(relative)
            except OSError as exc:
                print(f"  [suite] .rb_mkdirs {relative}: {exc}")
        if made:
            print(f"  [suite] .rb_mkdirs created: {', '.join(made)}")

    def launch_app(self, env: dict[str, str]) -> bool:
        out = self._app_out()
        self._restore_empty_fixture_dirs(env)
        if self.launch_cmd:
            electron_flags = self._electron_flags()
            suite_args = self._suite_launch_args(env)
            command = self.launch_cmd + electron_flags + suite_args
            if electron_flags:
                print(f"  [a11y] Electron candidate: appending{electron_flags}")
            if suite_args:
                print(f"  [suite] .rb_launch_args:{suite_args}")
            self.app = subprocess.Popen(
                command,
                shell=True,
                stdout=out,
                stderr=subprocess.STDOUT,
                env=env,
            )
            time.sleep(self.launch_wait)
            return self.app.poll() is None

        if not self.binary:
            return False
        for cmd in ([self.binary, "--no-sandbox"], [self.binary]):
            self.app = subprocess.Popen(
                cmd,
                stdout=out,
                stderr=subprocess.STDOUT,
                env=env,
            )
            time.sleep(self.launch_wait)
            if self.app.poll() is None:
                return True
            terminate_process(self.app)
        return False

    def dump_desktop(self, env: dict[str, str]) -> None:
        """Print the live AT-SPI desktop (apps + top frames). Called when app
        detection falls back, to reveal whether the launched app joined the a11y
        bus at all and under what name/role."""
        try:
            p = subprocess.run(
                [sys.executable, __file__, "--inspect-desktop"],
                capture_output=True,
                text=True,
                timeout=20,
                env=env,
            )
            print("  [desktop-dump]")
            for ln in (p.stdout or "").splitlines():
                print("    " + ln)
            if p.stderr.strip():
                print("    [dump-stderr] " + p.stderr.strip()[:400])
        except Exception as e:
            print("  [desktop-dump failed]", e)

    def detect_app_name(self, env: dict[str, str]) -> str:
        if not self.app or self.app.poll() is not None:
            return self.fallback_app_name
        try:
            proc = subprocess.run(
                [
                    sys.executable,
                    __file__,
                    "--inspect-app",
                    str(self.app.pid),
                    self.fallback_app_name,
                ],
                check=False,
                capture_output=True,
                text=True,
                timeout=10,
                env=env,
            )
            detected = proc.stdout.strip().splitlines()[-1]
            return detected or self.fallback_app_name
        except Exception:
            return self.fallback_app_name

    def cleanup(self) -> None:
        if self.app is not None:
            subprocess.run(
                ["pkill", "-P", str(self.app.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        terminate_process(self.app)
        self.app = None
        if self.use_native:
            return  # leave the shared native session (display/dbus/wm) running
        terminate_process(self.openbox)
        terminate_process(self.atspi)
        if self.dbus_pid:
            try:
                os.kill(self.dbus_pid, signal.SIGTERM)
            except Exception:
                pass
        terminate_process(self.xvfb)
        self.openbox = None
        self.atspi = None
        self.xvfb = None
        self.dbus_pid = None
        self.cleanup_stale_display()


def run_test_file(
    test_file: Path,
    app_name: str,
    results_dir: Path,
    env: dict[str, str],
    timeout: int,
) -> int:
    # Frozen Tier-1 suites are pytest modules. Their co-located conftest.py owns
    # the app/results fixtures and writes <module>_results.json from pytest hooks.
    # Executing the file directly merely defines its test functions and exits 0,
    # so no test is collected and no result JSON is produced.
    test_env = env.copy()
    test_env.update(
        {
            "RB_APP_NAME": app_name,
            "RB_RESULTS_DIR": str(results_dir),
            "RB_TESTGEN_DIR": str(test_file.parent),
        }
    )
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", str(test_file)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout,
        env=test_env,
    )
    lines = proc.stdout.splitlines()
    for line in lines[-8:]:
        print(line)
    return proc.returncode


def run_with_lifecycle(args: argparse.Namespace, test_files: list[Path]) -> None:
    session = GuiSession(
        display=args.display,
        screen=args.screen,
        binary=args.binary or args.reap_binary,
        launch_cmd=args.launch_cmd,
        fallback_app_name=args.app_name,
        launch_wait=args.launch_wait,
        use_native=args.use_native,
    )
    mode = "native desktop session" if args.use_native else "fresh GUI stack"
    try:
        for test_file in test_files:
            module = test_file.stem.replace("test_atspi_", "")
            print(f"=== Running {module} with {mode} on {args.display} ===")
            before = set(result_files(args.results))
            test_home = tempfile.mkdtemp(prefix="atspi-home.")
            session.app_diag = os.path.join(str(args.results), f"_app_{module}.log")
            try:
                session.start_stack()
                env = session.env.copy()
                env.update(
                    {
                        "HOME": test_home,
                        "XDG_CONFIG_HOME": os.path.join(test_home, ".config"),
                        "XDG_CACHE_HOME": os.path.join(test_home, ".cache"),
                        "XDG_DATA_HOME": os.path.join(test_home, ".local/share"),
                        "FIXTURES_DIR": str(args.fixtures),
                    }
                )
                for key in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_DATA_HOME"):
                    Path(env[key]).mkdir(parents=True, exist_ok=True)

                session.reset_app_instance()
                if not session.launch_app(env):
                    write_runner_failure(args.results, module, "app failed to start")
                    print(f"  FAIL {module}: app failed to start")
                    continue

                if session.app is not None:
                    # Frozen rb_atspi helpers that understand RB_APP_PID resolve the
                    # launched process family before considering an ambient bus node.
                    env["RB_APP_PID"] = str(session.app.pid)
                detected_app = session.detect_app_name(env)
                print(
                    f"  App PID={session.app.pid if session.app else '?'} name={detected_app}"
                )
                if detected_app == session.fallback_app_name:
                    # detection fell back: the app is not on the a11y bus under a
                    # process-tree match. Surface the live desktop + the app's own
                    # stdout/stderr so the failure mode (missing a11y bridge vs
                    # crash vs wrong window) is diagnosable from the log.
                    session.dump_desktop(env)
                    try:
                        if session.app_diag and os.path.exists(session.app_diag):
                            _ao = open(session.app_diag).read()[-1800:]
                            if _ao.strip():
                                print(f"  [app-output {module}]")
                                for _ln in _ao.splitlines():
                                    print("    " + _ln)
                    except Exception:
                        pass
                try:
                    run_test_file(
                        test_file,
                        detected_app,
                        args.results,
                        env,
                        args.module_timeout,
                    )
                except subprocess.TimeoutExpired:
                    write_runner_failure(args.results, module, "test timed out")
                    print(f"  FAIL {module}: test timed out")

                after = set(result_files(args.results))
                if not (after - before):
                    write_runner_failure(
                        args.results, module, "test produced no result json"
                    )
                    print(f"  FAIL {module}: test produced no result json")
            finally:
                session.cleanup()
                shutil.rmtree(test_home, ignore_errors=True)
    finally:
        session.cleanup()


def detect_existing_app_name(pid: int, fallback: str) -> str:
    try:
        proc = subprocess.run(
            [sys.executable, __file__, "--inspect-app", str(pid), fallback],
            capture_output=True,
            text=True,
            timeout=10,
        )
        lines = proc.stdout.strip().splitlines()
        return (lines[-1] if lines else "") or fallback
    except Exception:
        return fallback


def run_existing_process(args: argparse.Namespace, test_files: list[Path]) -> None:
    env = os.environ.copy()
    env["FIXTURES_DIR"] = str(args.fixtures)
    app_name = detect_existing_app_name(args.app_pid, args.app_name)
    print(
        "WARNING: --app-pid mode scores an existing process without lifecycle "
        "isolation; use --binary or --launch-cmd for canonical baseline/eval."
    )
    for test_file in test_files:
        module = test_file.stem.replace("test_atspi_", "")
        before = set(result_files(args.results))
        print(f"=== Running {module} against PID {args.app_pid} ({app_name}) ===")
        try:
            run_test_file(test_file, app_name, args.results, env, args.module_timeout)
        except subprocess.TimeoutExpired:
            write_runner_failure(args.results, module, "test timed out")
        after = set(result_files(args.results))
        if not (after - before):
            write_runner_failure(args.results, module, "test produced no result json")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    launcher = parser.add_mutually_exclusive_group(required=True)
    launcher.add_argument("--binary", help="Executable to launch for each test module")
    launcher.add_argument(
        "--launch-cmd", help="Shell command to launch for each module"
    )
    launcher.add_argument(
        "--app-pid", type=int, help="Existing app PID for quick scoring"
    )
    parser.add_argument(
        "--reap-binary",
        help="Executable to reap before each --launch-cmd module run",
    )
    parser.add_argument(
        "--tests", required=True, type=Path, help="Directory with tests"
    )
    parser.add_argument("--results", required=True, type=Path, help="Results directory")
    parser.add_argument("--fixtures", type=Path, help="Fixtures directory")
    parser.add_argument("--summary", default="programmatic_results.json")
    parser.add_argument("--app-name", default="")
    parser.add_argument("--display", default=":99")
    parser.add_argument("--screen", default="1920x1080x24")
    parser.add_argument(
        "--use-native-display",
        dest="use_native",
        action="store_true",
        help="Reuse the existing native desktop session (e.g. GNOME on :0) and "
        "its D-Bus/AT-SPI from the environment instead of building a throwaway "
        "Xvfb+openbox stack per module; only the app is relaunched per module. "
        "Gives real-desktop fidelity and runs apps needing GNOME services; "
        "trades per-module display isolation. Never tears down the session.",
    )
    parser.add_argument("--launch-wait", type=float, default=3.0)
    parser.add_argument(
        "--module-timeout",
        type=int,
        default=int(os.environ.get("RB_EVAL_MODULE_TIMEOUT", "300")),
        help="Per-test-module timeout in seconds (env: RB_EVAL_MODULE_TIMEOUT)",
    )
    parser.add_argument("--fail-under", type=float)
    parser.add_argument("--print-summary", action="store_true")
    parser.add_argument(
        "--manifest",
        help="Score the candidate run against this frozen manifest JSON "
        "(fixed denominator; missing tests injected as not_run)",
    )
    parser.add_argument(
        "--write-manifest",
        help="After a baseline run, freeze the canonical manifest to this path",
    )
    parser.add_argument("--task-id", default="")
    parser.add_argument("--baseline-model", default="")
    parser.add_argument("--baseline-commit", default="")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.tests = args.tests.resolve()
    args.results = args.results.resolve()
    args.fixtures = (args.fixtures or (args.results / "fixtures")).resolve()

    if args.binary and not Path(args.binary).exists():
        print(f"ERROR: binary not found: {args.binary}", file=sys.stderr)
        return 2
    if args.binary:
        args.binary = str(Path(args.binary).resolve())

    test_files = find_tests(args.tests)
    clean_results(args.results, args.summary)
    if not test_files:
        summary = collect_summary(args.results, test_files, args.display)
        summary["error"] = "no test files"
        (args.results / args.summary).write_text(json.dumps(summary, indent=2))
        print(json.dumps(summary))
        return 2

    print(f"Tests: {len(test_files)}")
    print(f"Results: {args.results}")
    print(f"Fixtures: {args.fixtures}")
    if args.launch_cmd:
        print(f"Launch command: {args.launch_cmd}")
    elif args.binary:
        print(f"Binary: {args.binary}")
    else:
        print(f"App PID: {args.app_pid}")

    try:
        if args.app_pid:
            run_existing_process(args, test_files)
        else:
            run_with_lifecycle(args, test_files)
    except Exception as exc:
        write_runner_failure(args.results, "runner", f"{type(exc).__name__}: {exc}")
        print(f"ERROR: runner failed: {exc}", file=sys.stderr)

    manifest_path = Path(args.manifest) if args.manifest else None
    if manifest_path and manifest_path.exists():
        manifest = json.loads(manifest_path.read_text())
        summary = score_against_manifest(args.results, manifest, args.display)
    else:
        summary = collect_summary(args.results, test_files, args.display)
        if args.write_manifest:
            manifest = build_manifest(
                args.results,
                task_id=args.task_id,
                baseline_model=args.baseline_model,
                baseline_commit=args.baseline_commit,
            )
            Path(args.write_manifest).write_text(json.dumps(manifest, indent=2))
            summary["manifest_total"] = manifest["total"]
            summary["manifest_sha"] = manifest["manifest_sha"]
            summary["vlm_total"] = manifest["vlm_total"]
    if summary.get("total", 0) == 0 and "error" not in summary:
        summary["error"] = "no result tests"
    summary_path = args.results / args.summary
    summary_path.write_text(json.dumps(summary, indent=2))

    compact = json.dumps(summary)
    print(
        f"Programmatic: {summary['passed']}/{summary['total']} "
        f"({summary['pass_rate']:.1%})"
    )
    if args.print_summary:
        print(compact)

    if summary.get("error") or summary["total"] == 0:
        if not args.print_summary:
            print(compact)
        # Exit 1 is the cross-platform RC_DATA contract.  It tells the outer
        # worker to inspect the result marker and retain this deterministic
        # candidate failure as a terminal zero.  Exit 2 remains reserved for
        # runner/display/evaluator failures that are safe to retry.
        if summary.get("error") == "app_launch_failed":
            return 1
        return 2
    if args.fail_under is not None and summary["pass_rate"] < args.fail_under:
        print(
            f"FAIL: pass rate {summary['pass_rate']:.1%} < " f"{args.fail_under:.1%}",
            file=sys.stderr,
        )
        if not args.print_summary:
            print(compact)
        return 1
    if not args.print_summary:
        print(compact)
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--inspect-desktop":
        inspect_atspi_desktop()
        sys.exit(0)
    if len(sys.argv) == 4 and sys.argv[1] == "--inspect-app":
        print(inspect_atspi_app_name(int(sys.argv[2]), sys.argv[3]))
        sys.exit(0)
    sys.exit(main())
