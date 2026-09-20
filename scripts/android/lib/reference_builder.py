#!/usr/bin/env python3
"""Build an Android reference APK from a wrapsource task descriptor.

The released Android task contains source identity and optional patches, but no APK.
This helper runs only in the trusted preparation phase.  Its working tree is removed
before the recreation agent starts; only the installed app survives for black-box
inspection.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[2]
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from core.reference_source import prepare_reference_source  # noqa: E402


_IGNORED_BUILD_PARTS = {".git", ".gradle", "build", "node_modules"}
_ANDROID_RUST_TARGETS = (
    "aarch64-linux-android",
    "armv7-linux-androideabi",
    "i686-linux-android",
    "x86_64-linux-android",
)


def _walk_source_files(source: Path):
    for root, directories, filenames in os.walk(source):
        directories[:] = sorted(name for name in directories if name not in _IGNORED_BUILD_PARTS)
        root_path = Path(root)
        for filename in sorted(filenames):
            yield root_path / filename


def _package_name(aapt: Path, apk: Path) -> str:
    result = subprocess.run(
        [str(aapt), "dump", "badging", str(apk)],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    if result.returncode:
        return ""
    match = re.search(r"package: name='([^']+)'", result.stdout)
    return match.group(1) if match else ""


def _gradle_roots(source: Path) -> list[Path]:
    wrapper_roots: set[Path] = set()
    settings_roots: set[Path] = set()
    for path in _walk_source_files(source):
        if path.name == "gradlew":
            wrapper_roots.add(path.parent)
        elif path.name in {"settings.gradle", "settings.gradle.kts"}:
            settings_roots.add(path.parent)
    roots = wrapper_roots or settings_roots
    return sorted(roots, key=lambda path: (len(path.relative_to(source).parts), str(path)))


def _write_local_properties(project: Path, android_sdk: Path) -> None:
    path = project / "local.properties"
    existing = (
        path.read_text(encoding="utf-8", errors="replace").splitlines() if path.is_file() else []
    )
    lines = [line for line in existing if not line.strip().startswith("sdk.dir=")]
    lines.append(f"sdk.dir={android_sdk.as_posix()}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _build_config_texts(source: Path) -> list[str]:
    texts = []
    for path in _walk_source_files(source):
        if not (
            path.name == "gradle.properties"
            or path.name.endswith((".gradle", ".gradle.kts", ".toml"))
            or ("buildSrc" in path.parts and path.suffix in {".java", ".kt"})
        ):
            continue
        try:
            texts.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return texts


def _quoted_versions(text: str, names: str) -> set[str]:
    return {
        match.group(1).strip()
        for match in re.finditer(
            rf"(?im)^\s*(?:{names})\s*(?:=|\s)\s*[\"']([^\"']+)[\"']",
            text,
        )
    }


def _required_sdk_packages(config_texts: list[str], android_sdk: Path) -> list[str]:
    platforms: set[int] = set()
    build_tools: set[str] = set()
    ndks: set[str] = set()
    cmakes: set[str] = set()
    for text in config_texts:
        for match in re.finditer(
            r"(?:compileSdk(?:Version)?|android-compileSdk|sdkCompile|currentSDK(?:Wear)?)"
            r"\s*(?:=|\s)\s*[\"']?(\d{2})",
            text,
            re.IGNORECASE,
        ):
            level = int(match.group(1))
            if 21 <= level <= 40:
                platforms.add(level)
        for match in re.finditer(
            r"(?:buildToolsVersion|buildTools|androidBuildTools)"
            r"\s*(?:=|\s)\s*[\"'](\d+\.\d+\.\d+)",
            text,
            re.IGNORECASE,
        ):
            build_tools.add(match.group(1))
        ndks.update(_quoted_versions(text, r"ndkVersion|sideBySideNdkVersion|ndk"))
        cmakes.update(_quoted_versions(text, r"cmake"))
        cmakes.update(
            match.group(1).strip()
            for match in re.finditer(
                r"(?s)\bcmake\s*\{.{0,600}?\bversion\s*=\s*[\"']([^\"']+)[\"']",
                text,
            )
        )

    packages = [
        f"platforms;android-{level}"
        for level in sorted(platforms)
        if not (android_sdk / "platforms" / f"android-{level}" / "android.jar").is_file()
    ]
    packages.extend(
        f"build-tools;{version}"
        for version in sorted(build_tools)
        if not (android_sdk / "build-tools" / version / "aapt2").is_file()
    )
    normalized_ndks = {version.split()[0] for version in ndks}
    packages.extend(
        f"ndk;{version}"
        for version in sorted(normalized_ndks)
        if not (android_sdk / "ndk" / version / "source.properties").is_file()
    )
    packages.extend(
        f"cmake;{version}"
        for version in sorted(cmakes)
        if re.fullmatch(r"\d+(?:\.\d+){1,3}", version)
        and not (android_sdk / "cmake" / version / "bin" / "cmake").is_file()
    )
    return packages


def _ensure_sdk_packages(config_texts: list[str], android_sdk: Path, env: dict[str, str]) -> None:
    packages = _required_sdk_packages(config_texts, android_sdk)
    if not packages:
        return
    sdkmanager = android_sdk / "cmdline-tools" / "latest" / "bin" / "sdkmanager"
    if not sdkmanager.is_file():
        raise FileNotFoundError(f"sdkmanager not found: {sdkmanager}")
    print(
        f"Installing Android SDK packages required by reference: {', '.join(packages)}", flush=True
    )
    subprocess.run(
        [str(sdkmanager), f"--sdk_root={android_sdk}", "--channel=3", *packages],
        env=env,
        input="y\n" * 100,
        text=True,
        check=True,
    )


def _required_java_version(config_texts: list[str]) -> int:
    versions = {17}
    patterns = (
        r"JavaVersion\.VERSION_(?:1_)?(\d+)",
        r"JvmTarget\.JVM_(?:1_)?(\d+)",
        r"jvmToolchain\s*\(\s*(\d+)",
        r"JavaLanguageVersion\.of\s*\(\s*(\d+)",
        r"jvmTarget\s*=\s*[\"']?(\d+)",
        r"(?i)\bjdkVersion\s*=\s*(\d+)",
    )
    for text in config_texts:
        for pattern in patterns:
            versions.update(int(match.group(1)) for match in re.finditer(pattern, text))
    return max(versions)


def _configure_java(config_texts: list[str], env: dict[str, str]) -> None:
    required = _required_java_version(config_texts)
    if required <= 17:
        return
    if required > 21:
        raise RuntimeError(f"reference source requires unsupported JDK {required}")

    java_home = Path(env.get("RB_ANDROID_JAVA_21_HOME") or "/opt/java/openjdk")
    javac = java_home / "bin" / "javac"
    if not javac.is_file():
        raise FileNotFoundError(
            f"reference source requires JDK {required}, but JDK 21 was not found at {java_home}"
        )
    result = subprocess.run(
        [str(javac), "-version"],
        check=True,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    match = re.search(r"javac\s+(\d+)", result.stdout)
    if not match or int(match.group(1)) < required:
        raise RuntimeError(f"{javac} does not provide the required JDK {required}")
    env["JAVA_HOME"] = str(java_home)
    env["PATH"] = f"{java_home / 'bin'}:{env.get('PATH', '')}"
    print(f"Using JDK {match.group(1)} for reference source requiring Java {required}", flush=True)


def _prepare_rust(source: Path, env: dict[str, str], timeout_sec: int) -> None:
    source_files = list(_walk_source_files(source))
    cargo_projects = [path for path in source_files if path.name == "Cargo.toml"]
    if not cargo_projects:
        return
    for command in ("cargo", "rustup"):
        if not shutil.which(command, path=env.get("PATH")):
            raise FileNotFoundError(
                f"reference source contains Cargo.toml but {command} is not installed"
            )

    # Keep downloaded crates private to the trusted staging directory. The Rust
    # compiler and targets are image-level tools, but upstream dependency sources
    # must not survive into the recreation agent's environment.
    cargo_home = source.parent / "reference-cargo-home"
    cargo_home.mkdir(parents=True, exist_ok=True)
    env["CARGO_HOME"] = str(cargo_home)
    subprocess.run(
        ["rustup", "target", "add", *_ANDROID_RUST_TARGETS],
        env=env,
        check=True,
        timeout=timeout_sec,
    )

    setup_scripts = [path for path in source_files if path.name == "setup_rust_android.sh"]
    for script in sorted(setup_scripts):
        print(f"Preparing Android Rust toolchain with {script}", flush=True)
        subprocess.run(
            ["bash", str(script)],
            cwd=script.parent,
            env=env,
            check=True,
            timeout=timeout_sec,
        )


def _candidate_apks(source: Path) -> list[Path]:
    candidates = []
    for apk in source.rglob("*.apk"):
        lowered = {part.lower() for part in apk.relative_to(source).parts}
        name = apk.name.lower()
        if (
            "build" not in lowered
            or "androidtest" in lowered
            or "test" in lowered
            or "unsigned" in name
        ):
            continue
        candidates.append(apk)
    return sorted(
        candidates,
        key=lambda path: (
            "debug" not in path.name.lower(),
            len(path.relative_to(source).parts),
            str(path),
        ),
    )


def _find_matching_apk(source: Path, aapt: Path, package: str) -> Path | None:
    debug_variant = None
    for apk in _candidate_apks(source):
        actual = _package_name(aapt, apk)
        if actual == package:
            return apk
        # Some upstreams add this suffix to assembleDebug outputs. The pipeline
        # reads the selected APK's actual ID before installing/launching it.
        # Never accept arbitrary package prefixes or prefer a variant to an exact match.
        if actual == f"{package}.debug" and debug_variant is None:
            debug_variant = apk
    return debug_variant


def build_reference_apk(
    descriptor_path: Path,
    reference_dir: Path,
    source_dir: Path,
    output_apk: Path,
    aapt: Path,
    timeout_sec: int = 7200,
) -> Path:
    descriptor = json.loads(descriptor_path.read_text(encoding="utf-8-sig"))
    if descriptor.get("platform") != "android":
        raise ValueError("reference APK builder requires an Android descriptor")
    package = str(descriptor.get("package") or "").strip()
    if not package:
        raise ValueError("Android descriptor has no package")
    if not aapt.is_file():
        raise FileNotFoundError(f"aapt not found: {aapt}")

    prepare_reference_source(
        descriptor_path,
        source_dir,
        reference_dir / "reference.tar.gz",
    )
    roots = _gradle_roots(source_dir)
    if not roots:
        raise RuntimeError("source checkout contains no Gradle project")

    android_sdk = Path(os.environ.get("ANDROID_HOME") or os.environ.get("ANDROID_SDK_ROOT") or "")
    if not android_sdk.is_dir():
        raise RuntimeError("ANDROID_HOME/ANDROID_SDK_ROOT does not name an installed SDK")

    env = dict(os.environ)
    # The recreation toolchain deliberately forces agent builds offline. Reference
    # projects have their own dependency graphs, so prepare them in a private cache
    # while trusted setup still has network access.
    gradle_home = source_dir.parent / "reference-gradle-home"
    gradle_home.mkdir(parents=True, exist_ok=True)
    env["GRADLE_USER_HOME"] = str(gradle_home)
    env["ANDROID_HOME"] = str(android_sdk)
    env["ANDROID_SDK_ROOT"] = str(android_sdk)
    config_texts = _build_config_texts(source_dir)
    _configure_java(config_texts, env)
    _ensure_sdk_packages(config_texts, android_sdk, env)
    _prepare_rust(source_dir, env, timeout_sec)

    failures: list[str] = []
    for project in roots:
        _write_local_properties(project, android_sdk)
        wrapper = project / "gradlew"
        if wrapper.is_file():
            command = ["bash", str(wrapper)]
        else:
            gradle = shutil.which("gradle", path=env.get("PATH"))
            if not gradle:
                failures.append(f"{project}: source has no wrapper and Gradle is not installed")
                continue
            command = [gradle]
        print(f"Building Android reference with {' '.join(command)} assembleDebug", flush=True)
        try:
            result = subprocess.run(
                [*command, "--no-daemon", "--stacktrace", "assembleDebug"],
                cwd=project,
                env=env,
                check=False,
                timeout=timeout_sec,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            failures.append(f"{project}: {exc}")
        else:
            if result.returncode:
                failures.append(f"{project}: Gradle exited {result.returncode}")
        apk = _find_matching_apk(source_dir, aapt, package)
        if apk is not None:
            output_apk.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(apk, output_apk)
            actual_package = _package_name(aapt, apk)
            print(
                f"Reference APK built for {package} (application ID: {actual_package}): {apk}",
                flush=True,
            )
            return output_apk

    found = [
        f"{apk} ({_package_name(aapt, apk) or 'unreadable'})" for apk in _candidate_apks(source_dir)
    ]
    detail = "; ".join(failures) or "no APK matched the declared package or its debug variant"
    produced = ", ".join(found) or "none"
    raise RuntimeError(
        f"could not build an APK for declared package {package}; "
        f"build failures: {detail}; APKs produced: {produced}"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--descriptor", required=True, type=Path)
    parser.add_argument("--reference-dir", required=True, type=Path)
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--output-apk", required=True, type=Path)
    parser.add_argument("--aapt", required=True, type=Path)
    parser.add_argument("--timeout-sec", type=int, default=7200)
    args = parser.parse_args(argv)
    build_reference_apk(
        args.descriptor,
        args.reference_dir,
        args.source_dir,
        args.output_apk,
        args.aapt,
        args.timeout_sec,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
