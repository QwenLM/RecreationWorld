#!/usr/bin/env python3
"""Import every platform from the bundled provider-neutral runtime."""

from __future__ import annotations

import hashlib
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = ROOT
sys.path[:0] = [str(RUNTIME / "scripts"), str(RUNTIME / "src")]

from common.rb_unify.eval_bridge import copy_unified_from_local, stage_instance  # noqa: E402
from common.rb_unify.reference import validate_reference  # noqa: E402
from android.lib.reference_builder import (  # noqa: E402
    _build_config_texts,
    _gradle_roots,
    _required_java_version,
    _required_sdk_packages,
)
from core import pipeline  # noqa: E402
from infrastructure import artifacts  # noqa: E402
from recreation_bench.task import TaskInstance  # noqa: E402


def check_wrapsource_contract(root: Path) -> None:
    patch_bytes = b"diff --git a/a b/a\n"
    descriptor = {
        "schema_version": 1,
        "instance_id": "android/example-app",
        "platform": "android",
        "repo": "https://example.com/example/app.git",
        "commit": "a" * 40,
        "patches": [
            {
                "path": "reference/patches/local-source.patch",
                "reason": "build_fix",
                "note": "smoke fixture",
                "sha256": hashlib.sha256(patch_bytes).hexdigest(),
            }
        ],
        "package": "org.example.app",
        "license": None,
    }
    parsed = TaskInstance.model_validate(descriptor)
    if parsed.patches[0].reason != "build_fix":
        raise RuntimeError("typed patch metadata was not preserved")

    reference = root / "source-only-reference"
    (reference / "patches").mkdir(parents=True)
    (reference / "launch.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (reference / "patches" / "local-source.patch").write_bytes(patch_bytes)
    errors = validate_reference(reference, "android", patches=descriptor["patches"])
    if errors:
        raise RuntimeError(f"valid source-only reference rejected: {errors}")

    web_reference = root / "web-reference"
    web_reference.mkdir()
    (web_reference / "launch.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    if not any(
        "reference.tar.gz missing" in error for error in validate_reference(web_reference, "web")
    ):
        raise RuntimeError("web reference without its frozen-site archive was accepted")

    android_source = root / "android-source"
    (android_source / "buildSrc").mkdir(parents=True)
    (android_source / "gradle").mkdir()
    (android_source / "settings.gradle.kts").write_text('rootProject.name = "smoke"\n')
    (android_source / "buildSrc" / "Versions.kt").write_text(
        "const val CompileSDK = 36\nconst val JdkVersion = 21\n"
    )
    (android_source / "gradle" / "tools.versions.toml").write_text(
        '[versions]\nbuildTools = "36.0.0"\nndk = "29.0.14206865"\ncmake = "4.1.2"\n'
    )
    texts = _build_config_texts(android_source)
    packages = set(_required_sdk_packages(texts, root / "empty-sdk"))
    expected = {
        "platforms;android-36",
        "build-tools;36.0.0",
        "ndk;29.0.14206865",
        "cmake;4.1.2",
    }
    if packages != expected or _required_java_version(texts) != 21:
        raise RuntimeError(f"Android source requirements were not detected: {packages}")
    if _gradle_roots(android_source) != [android_source]:
        raise RuntimeError("wrapperless Android Gradle project was not detected")

    staged = root / "staged"
    source_instance = root / "source-instance"
    (source_instance / "tests").mkdir(parents=True)
    (source_instance / "reference" / "patches").mkdir(parents=True)
    (source_instance / "reference" / "launch.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (source_instance / "reference" / "patches" / "local-source.patch").write_bytes(patch_bytes)
    stage_instance(source_instance, staged)
    if not (staged / "reference" / "patches" / "local-source.patch").is_file():
        raise RuntimeError("staging dropped a declared source patch")


def main() -> int:
    for platform in ("linux", "windows", "macos", "android", "web"):
        module = pipeline.load_platform(platform)
        if not callable(module.run):
            raise RuntimeError(f"platform entry is not callable: {platform}")
    store = artifacts.artifact_store_from_environment(
        {"RB_ARTIFACT_BACKEND": "filesystem", "RB_ARTIFACT_ROOT": "/tmp/rb-public-smoke"}
    )
    if type(store).__name__ != "FilesystemArtifactStore":
        raise RuntimeError(f"unexpected public artifact backend: {type(store).__name__}")
    with tempfile.TemporaryDirectory(prefix="rb-runtime-local-input-") as directory:
        root = Path(directory)
        check_wrapsource_contract(root)
        task = root / "windows" / "smoke-windows"
        (task / "reference").mkdir(parents=True)
        (task / "tests").mkdir()
        (task / "instance.json").write_text('{"platform":"windows"}\n')
        report = copy_unified_from_local(
            root,
            "windows",
            "smoke-windows",
            root / "materialized",
            components=("reference", "tests"),
        )
        if not report.get("prefix"):
            raise RuntimeError("bundled runtime could not materialize local Windows input")
    print("Bundled five-platform runtime smoke passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
