#!/usr/bin/env python3
"""Offline smoke checks for the external five-platform controller bundle."""

from __future__ import annotations

import compileall
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROVIDERS = ROOT / "providers"
sys.path.insert(0, str(PROVIDERS))

from common.controller_helper import render_codex_config  # noqa: E402
from common.runtime_source import add_runtime_source, safe_extract_tar  # noqa: E402
from common.unified_cache import prepare_unified_archive, validate_unified_archive  # noqa: E402


def run(command: list[str], *, cwd: Path = ROOT, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(command, cwd=cwd, env=env, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(
            f"command failed ({result.returncode}): {' '.join(command)}\n"
            + result.stdout
            + result.stderr
        )
    return result.stdout


def create_input(root: Path, platform: str, task_id: str) -> Path:
    task = root / platform / task_id
    (task / "reference").mkdir(parents=True)
    (task / "tests").mkdir()
    (task / "instance.json").write_text(
        json.dumps({"instance_id": task_id, "platform": platform}) + "\n",
        encoding="utf-8",
    )
    (task / "reference" / "payload.txt").write_text("reference\n", encoding="utf-8")
    (task / "tests" / "payload.txt").write_text("tests\n", encoding="utf-8")
    if platform == "web":
        (task / "reference" / "task.json").write_text(
            '{"task":"smoke"}\n', encoding="utf-8"
        )
        (task / "reference" / "site_meta.json").write_text(
            '{"pages":[]}\n', encoding="utf-8"
        )
        with tarfile.open(task / "reference" / "reference.tar.gz", "w:gz") as archive:
            content = b"<html><body>smoke</body></html>\n"
            info = tarfile.TarInfo("site/index.html")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        (task / "tests" / "eval_config.json").write_text("{}\n", encoding="utf-8")
        (task / "tests" / "static").mkdir()
        (task / "tests" / "static" / "content.scripted.spec.ts").write_text(
            "// smoke\n", encoding="utf-8"
        )
    return task


def check_local_inputs(temp: Path) -> None:
    dataset = temp / "dataset"
    for platform in ("ubuntu", "windows", "macos", "android", "web"):
        task_id = f"smoke-{platform}"
        create_input(dataset, platform, task_id)
        archive = temp / "archives" / f"{task_id}.{platform}.tar.gz"
        selected = prepare_unified_archive(
            task_id=task_id,
            platform=platform,
            archive=None,
            cache_dir=dataset,
            output_archive=archive,
        )
        assert selected == archive
        assert validate_unified_archive(archive, task_id=task_id, platform=platform) == task_id

    try:
        prepare_unified_archive(
            task_id="missing",
            platform="web",
            archive=None,
            cache_dir=dataset,
            output_archive=temp / "missing.tar.gz",
        )
    except RuntimeError as exc:
        assert "provide --unified-archive or --unified-cache-dir" in str(exc)
    else:
        raise AssertionError("missing local input unexpectedly succeeded")


def check_group_dry_runs(temp: Path) -> None:
    dataset = temp / "dataset"
    cases = (
        ("linux", "ubuntu"),
        ("web", "web"),
        ("windows", "windows"),
    )
    for platform, dataset_platform in cases:
        task_id = f"smoke-{dataset_platform}"
        command = [
            sys.executable,
            str(PROVIDERS / platform / "template" / "run_many.py"),
            "--task-id",
            task_id,
            "--unified-cache-dir",
            str(dataset),
            "--out-dir",
            str(temp / "dry-run" / platform),
            "--dry-run",
        ]
        output = run(command)
        assert task_id in output
        assert str(dataset) in output


def check_controller_helper(temp: Path) -> None:
    helper = PROVIDERS / "common" / "controller_helper.py"
    run([sys.executable, str(helper), "check-modules", "json", "pathlib"])

    missing_input = subprocess.run(
        [
            sys.executable,
            str(helper),
            "materialize-web-input",
            "--task-id",
            "smoke-web",
            "--instance-dir",
            str(temp / "missing-instance"),
            "--dataset-dir",
            str(temp / "missing-dataset"),
            "--local-root",
            "",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
    )
    assert missing_input.returncode != 0
    assert "RB_UNIFIED_LOCAL_ROOT is required" in missing_input.stderr

    web_instance = temp / "web-instance"
    web_dataset = temp / "web-dataset"
    helper_env = dict(os.environ)
    runtime_scripts = str(ROOT / "scripts")
    helper_env["PYTHONPATH"] = (
        runtime_scripts
        + (os.pathsep + helper_env["PYTHONPATH"] if helper_env.get("PYTHONPATH") else "")
    )
    domain = run(
        [
            sys.executable,
            str(helper),
            "materialize-web-input",
            "--task-id",
            "smoke-web",
            "--instance-dir",
            str(web_instance),
            "--dataset-dir",
            str(web_dataset),
            "--local-root",
            str(temp / "dataset"),
        ],
        env=helper_env,
    ).strip().splitlines()[-1]
    assert (web_dataset / domain / "task.json").is_file()

    parts = temp / "parts"
    parts.mkdir()
    (parts / "part-0000").write_bytes(b"first")
    (parts / "part-0001").write_bytes(b"second")
    joined = temp / "joined.bin"
    run(
        [
            sys.executable,
            str(helper),
            "join-parts",
            "--parts-dir",
            str(parts),
            "--output",
            str(joined),
        ]
    )
    assert joined.read_bytes() == b"firstsecond"

    metrics = temp / "visual-metrics.json"
    run(
        [
            sys.executable,
            str(helper),
            "write-visual-metrics",
            "--task-id",
            "smoke-linux",
            "--platform",
            "linux",
            "--output",
            str(metrics),
        ]
    )
    payload = json.loads(metrics.read_text())
    assert payload["passed"] is True and payload["visual_only"] is True

    output = temp / "linux-output"
    task_root = output / "pipeline_state" / "smoke-linux"
    recreation = task_root / "recreation_model" / "recreation"
    evaluation = task_root / "eval"
    recreation.mkdir(parents=True)
    evaluation.mkdir()
    (output / "metrics.json").write_text('{"passed":true}\n')
    (recreation / "artifact.txt").write_text("artifact\n")
    (evaluation / "score.txt").write_text("score\n")
    bundle = temp / "result_artifacts.tar.gz"
    run(
        [
            sys.executable,
            str(helper),
            "pack-linux-results",
            "--task-id",
            "smoke-linux",
            "--output-dir",
            str(output),
            "--bundle",
            str(bundle),
        ]
    )
    with tarfile.open(bundle, "r:gz") as archive:
        names = set(archive.getnames())
    assert "metrics.json" in names
    assert "pipeline_state/recreation_model/recreation/artifact.txt" in names
    assert "recreation/artifact.txt" in names
    assert "eval/score.txt" in names


def check_codex_config_rendering() -> None:
    """Keep the public provider contract aligned with the shared Codex renderer."""
    scripts = str(ROOT / "scripts")
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    rendered = render_codex_config(
        model="openai.gpt-5.6-sol[1m]",
        base_url="https://api.example.com/v1",
        platform="macos",
    )
    assert 'model = "gpt-5.6-sol"' in rendered
    assert 'model_provider = "recreationbench"' in rendered
    assert 'base_url = "https://api.example.com/v1"' in rendered
    assert 'wire_api = "responses"' in rendered


def check_runtime_source_archive(temp: Path) -> None:
    """Local state and credentials must never ride with a deployed runtime."""

    source = temp / "runtime-source"
    (source / "scripts" / "core").mkdir(parents=True)
    (source / "scripts" / "release").mkdir()
    (source / "scripts" / "__pycache__").mkdir()
    (source / "src" / "recreation_bench").mkdir(parents=True)
    (source / "src" / "recreation_bench.egg-info").mkdir()
    (source / "build").mkdir()
    (source / "scripts" / "core" / "runtime.py").write_text("# runtime\n")
    (source / "src" / "recreation_bench" / "__init__.py").write_text("")
    (source / "pyproject.toml").write_text("[project]\nname='smoke'\n")
    (source / ".env.linux").write_text("SECRET=do-not-copy\n")
    (source / "scripts" / ".env.runtime").write_text("SECRET=do-not-copy\n")
    (source / "scripts" / "release" / "audit.py").write_text("# controller only\n")
    (source / "scripts" / "__pycache__" / "runtime.pyc").write_bytes(b"cache")
    (source / "src" / "recreation_bench.egg-info" / "PKG-INFO").write_text("cache\n")
    (source / "build" / "artifact.txt").write_text("cache\n")

    bundle = temp / "runtime.tar.gz"
    with tarfile.open(bundle, "w:gz") as archive:
        add_runtime_source(archive, source, archive_root="RecreationBench")
    with tarfile.open(bundle, "r:gz") as archive:
        names = set(archive.getnames())

    assert "RecreationBench/scripts/core/runtime.py" in names
    assert "RecreationBench/src/recreation_bench/__init__.py" in names
    assert "RecreationBench/pyproject.toml" in names
    assert not any(".env" in name for name in names)
    assert not any("__pycache__" in name for name in names)
    assert not any("egg-info" in name for name in names)
    assert not any(name.startswith("RecreationBench/scripts/release") for name in names)
    assert not any(name.startswith("RecreationBench/build") for name in names)


def check_safe_archive_extraction(temp: Path) -> None:
    """Agent-produced archives must not write outside the result directory."""

    good = temp / "good-results.tar.gz"
    with tarfile.open(good, "w:gz") as archive:
        content = b'{"passed": true}\n'
        info = tarfile.TarInfo("results/metrics.json")
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
    extracted = temp / "safe-results"
    safe_extract_tar(good, extracted)
    assert (extracted / "results" / "metrics.json").is_file()

    for filename in ("../outside.txt", "..\\outside.txt", "C:/outside.txt"):
        malicious = temp / (filename.replace("/", "-").replace("\\", "-") + ".tar.gz")
        with tarfile.open(malicious, "w:gz") as archive:
            content = b"escape\n"
            info = tarfile.TarInfo(filename)
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
        try:
            safe_extract_tar(malicious, temp / "unsafe-results")
        except RuntimeError as exc:
            assert "unsafe archive path" in str(exc)
        else:
            raise AssertionError(f"unsafe result archive unexpectedly extracted: {filename}")
    assert not (temp / "outside.txt").exists()


def check_syntax() -> None:
    if not compileall.compile_dir(ROOT, quiet=1):
        raise RuntimeError("Python byte compilation failed")
    for path in sorted(ROOT.rglob("*")):
        if (
            not path.is_file()
            or ".git" in path.parts
            or any(part in {".venv", "__pycache__"} for part in path.parts)
        ):
            continue
        first = path.read_bytes()[:128]
        if (
            path.suffix == ".sh"
            or first.startswith(b"#!/usr/bin/env bash")
            or first.startswith(b"#!/bin/bash")
        ):
            run(["bash", "-n", str(path)])


def main() -> int:
    check_syntax()
    check_codex_config_rendering()
    with tempfile.TemporaryDirectory(prefix="rb-public-smoke-") as directory:
        temp = Path(directory)
        check_local_inputs(temp)
        check_group_dry_runs(temp)
        check_controller_helper(temp)
        check_runtime_source_archive(temp)
        check_safe_archive_extraction(temp)
    print("Five-platform controller smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
