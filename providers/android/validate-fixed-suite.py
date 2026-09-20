#!/usr/bin/env python3
"""Validate a local Android evaluation dataset without running its tests."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))

from common.rb_unify.instance import validate_instance  # noqa: E402
from common.rb_unify.reference import validate_reference  # noqa: E402


def load_apps(path: Path) -> list[str]:
    apps = []
    for line_number, line in enumerate(path.read_text().splitlines(), start=1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            app_id = row.get("app_id") or row["instance_id"]
        except (json.JSONDecodeError, KeyError, TypeError, AttributeError) as exc:
            raise ValueError(f"invalid task entry at {path}:{line_number}: {exc}") from exc
        apps.append(app_id)
    if len(apps) != len(set(apps)):
        raise ValueError(f"duplicate app IDs in {path}")
    return apps


def read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid JSON file {path}: {exc}") from exc


def validate_app(dataset_dir: Path, app_id: str) -> tuple[int, int]:
    root = dataset_dir / "android" / app_id
    instance_path = root / "instance.json"
    reference_dir = root / "reference"
    tests_dir = root / "tests"
    manifest_path = tests_dir / "test_manifest.json"
    required = (
        instance_path,
        reference_dir / "launch.sh",
        manifest_path,
        tests_dir / "android_testgen_kit.py",
        root / "vlm_assertions.json",
    )
    missing = [str(path.relative_to(dataset_dir)) for path in required if not path.is_file()]
    if missing:
        raise ValueError(f"{app_id}: missing files: {', '.join(missing)}")

    instance = read_json(instance_path)
    if not isinstance(instance, dict):
        raise ValueError(f"{app_id}: instance.json must contain an object")
    if instance.get("instance_id") != f"android/{app_id}":
        raise ValueError(f"{app_id}: instance_id must be android/{app_id}")
    descriptor_errors = validate_instance(instance)
    reference_errors = validate_reference(
        str(reference_dir), "android", patches=instance.get("patches")
    )
    if descriptor_errors or reference_errors:
        details = "; ".join(descriptor_errors + reference_errors)
        raise ValueError(f"{app_id}: invalid wrapsource metadata: {details}")

    manifest = read_json(manifest_path)
    if not isinstance(manifest, dict) or manifest.get("manifest_version") != 1:
        raise ValueError(f"{app_id}: invalid test manifest")
    tests = manifest.get("tests")
    total = manifest.get("total")
    if not isinstance(tests, list) or not isinstance(total, int) or total <= 0:
        raise ValueError(f"{app_id}: invalid tests or total in test manifest")
    if total != len(tests):
        raise ValueError(f"{app_id}: manifest total={total}, tests={len(tests)}")
    names = [item.get("name") for item in tests if isinstance(item, dict)]
    if len(names) != len(tests) or any(not name for name in names):
        raise ValueError(f"{app_id}: manifest contains an invalid test entry")
    if len(names) != len(set(names)):
        raise ValueError(f"{app_id}: manifest contains duplicate test names")

    modules = sorted(tests_dir.glob("test_android_*.py"))
    if not modules:
        raise ValueError(f"{app_id}: tests directory contains no test_android_*.py")
    for module in modules:
        compile(module.read_text(), str(module), "exec")

    read_json(root / "vlm_assertions.json")
    return total, len(modules)


def main() -> int:
    script_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset-dir", type=Path, default=script_dir / "dataset")
    parser.add_argument("--app-id", action="append", default=[])
    parser.add_argument(
        "--tasks-file",
        type=Path,
        default=script_dir.parent.parent / "tasks/android.jsonl",
    )
    args = parser.parse_args()

    try:
        released_apps = load_apps(args.tasks_file)
        apps = args.app_id or released_apps
        unknown = sorted(set(apps) - set(released_apps))
        if unknown:
            raise ValueError(f"apps are outside the released suite: {', '.join(unknown)}")
        failures: list[str] = []
        total_tests = 0
        total_modules = 0
        for app_id in apps:
            try:
                tests, modules = validate_app(args.dataset_dir, app_id)
                total_tests += tests
                total_modules += modules
                print(f"OK {app_id}: tests={tests} modules={modules}")
            except Exception as exc:
                failures.append(f"{app_id}: {exc}")
                print(f"FAIL {app_id}: {exc}", file=sys.stderr)
        print(
            json.dumps(
                {
                    "apps_expected": len(apps),
                    "apps_valid": len(apps) - len(failures),
                    "test_cases": total_tests,
                    "test_modules": total_modules,
                    "failures": failures,
                },
                indent=2,
            )
        )
        return 1 if failures else 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
