#!/usr/bin/env python3
"""Frozen-manifest, fixed-denominator scoring — shared across all 5 platforms.

This is the single canonical implementation of the eval-result model that
linux/macos/windows/android all reimplemented separately:

  * parse heterogeneous ``*_results.json`` (tests-as-list / tests-as-dict /
    summary / passed+total) into canonical ``{module::test: status}``,
  * freeze the baseline-passing set into a manifest (the fixed denominator),
  * score a candidate against that manifest — a manifest test the candidate
    never reported is injected as ``not_run`` (a non-pass), so the denominator
    equals the manifest size for every candidate instead of shrinking.

It also reads the VLM-assertion set (``assertions.json`` ∪
``screenshot_asserts.jsonl``) so the manifest carries both the programmatic
cases (``tests``) and the VLM cases (``vlm_assertions``) — the unified testcase
form. Platform-specific test *running* (subprocess/GUI isolation/probe) stays in
each platform adapter; this module is pure parsing + scoring and imports nothing
platform-specific.

Promoted verbatim (behavior-preserving) from
scripts/linux/tools/atspi_test_runner.py.

WHO ACTUALLY IMPORTS THIS, because the previous version of this docstring claimed "every
adapter imports one copy" and no adapter did:

  * scripts/windows/stages/uia_eval.py  — delegates _iter_result_statuses() here.

And who CANNOT, for a structural reason rather than an oversight:

  * scripts/linux/tools/atspi_test_runner.py and Android's frozen eval kit are SHIPPED
    STANDALONE onto the VM / emulator host (the Linux eval stage copies the runner to
    run_atspi_tests.py; the Android kit is vendored per instance). Neither can import from a
    package that is not on that machine, so their copies stay by necessity. Deleting
    those copies would break the runners; the honest state is one shared copy for
    everything that runs where `core` exists, and documented duplicates where it does not.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

PASS_STATUSES = {"PASS", "PASSED", "OK", "SUCCESS"}
# Result files whose names start with these are aggregate/derived outputs, not
# per-module test results — skip them when collecting statuses.
SKIP_PREFIXES = ("programmatic", "vlm", "atspi_eval", "baseline_results")
CANONICAL_STATUSES = {"passed", "failed", "error", "skipped", "not_run"}


def result_files(results_dir: Path) -> list[Path]:
    files: list[Path] = []
    for pattern in (
        "*_results.json",
        "*_eval.json",
        "results/*_results.json",
        "results/*_eval.json",
    ):
        files.extend(Path(results_dir).glob(pattern))
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
    """(passed, total) from any of the shapes platforms emit."""
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
    name = Path(path).name
    for suffix in ("_results.json", "_eval.json"):
        if name.endswith(suffix):
            name = name[: -len(suffix)]
    # Historical filename prefixes in frozen suites are stripped so module keys are comparable.
    for prefix in (
        "test_atspi_",
        "atspi_",
        "test_uia_",
        "uia_",
        "test_ax_",
        "ax_",
        "test_",
    ):
        if name.startswith(prefix):
            name = name[len(prefix) :]
            break
    return name


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

    On duplicate names a ``passed`` wins over a non-pass; otherwise later files
    overwrite earlier ones.
    """
    statuses: dict[str, str] = {}
    for path in result_files(results_dir):
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
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

    Union of the append-only ``screenshot_asserts.jsonl`` key set (survives the
    read-modify-write clobbering that truncates ``assertions.json`` across
    per-module processes) with ``assertions.json`` (canonical text). Description
    prefers ``assertions.json``.
    """
    results_dir = Path(results_dir)
    jsonl_desc: dict[str, str] = {}
    for jsonl in (
        results_dir / "screenshots" / "screenshot_asserts.jsonl",
        results_dir / "screenshot_asserts.jsonl",
    ):
        if not jsonl.exists():
            continue
        for line in jsonl.read_text(encoding="utf-8-sig").splitlines():
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
            data = json.loads(cand.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        if isinstance(data, dict):
            for key, val in data.items():
                dict_desc.setdefault(str(key), str(val))
    out: dict[str, str] = {}
    for key in set(jsonl_desc) | set(dict_desc):
        out[key] = dict_desc.get(key) or jsonl_desc.get(key, "")
    return dict(sorted(out.items()))


def build_manifest(
    baseline_results_dir: Path,
    *,
    task_id: str = "",
    baseline_model: str = "",
    baseline_commit: str = "",
) -> dict:
    """Freeze the canonical (baseline-passing) test set into a manifest dict.

    Only tests that PASS on the original build enter ``tests`` (the fixed
    denominator); baseline-failing tests are recorded in ``ignored`` with reason
    ``baseline_fail``.
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
    every candidate. Candidate tests not in the manifest are ``unexpected`` and
    excluded from the score.
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
    return {
        "passed": passed,
        "total": total,
        "pass_rate": round(passed / total, 4) if total else 0.0,
        "by_module": by_module,
        "scored_against": "manifest",
        "manifest_sha": manifest.get("manifest_sha", ""),
        "manifest_total": manifest.get("total", total),
        "n_injected_not_run": injected,
        "n_unexpected": len(unexpected),
        "unexpected": unexpected[:50],
        "per_test": per_test,
        "display": display,
    }


def merge_vlm_assertion_logs(dst_path, src_path) -> int:
    """Merge a VLM audit log INTO another, dropping exact duplicates. Returns the merged count.

    Merge, never overwrite: the destination copy can come from a different round of the same
    suite, and ``build_manifest``'s ``vlm_unverified`` check reads this log -- an overwrite drops
    verified cases and they get misjudged as never having been VLM-checked.

    Both sides may be absent, empty, corrupt, or (wrongly) a dict instead of a list; each of
    those contributes nothing rather than raising. Order is destination-first, then source, and
    duplicates are decided on the whole entry.
    """

    def _load(path) -> list:
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        return data if isinstance(data, list) else []

    merged: list = []
    seen: set[str] = set()
    for entry in _load(dst_path) + _load(src_path):
        key = json.dumps(entry, sort_keys=True, ensure_ascii=False)
        if key not in seen:
            seen.add(key)
            merged.append(entry)
    Path(dst_path).write_text(
        json.dumps(merged, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return len(merged)


def _main(argv: list[str]) -> int:
    """CLI for the shell callers: `python3 -m core.manifest merge-vlm-log <dst> <src>`."""
    import argparse

    ap = argparse.ArgumentParser(prog="core.manifest")
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("merge-vlm-log")
    m.add_argument("dst")
    m.add_argument("src")
    args = ap.parse_args(argv)
    merge_vlm_assertion_logs(args.dst, args.src)
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_main(sys.argv[1:]))
