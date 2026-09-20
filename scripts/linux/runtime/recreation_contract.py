#!/usr/bin/env python3
"""Validate a Linux recreation directory without embedding Python in shell stages."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
from pathlib import Path

FORBIDDEN_BUILD_COMMAND = re.compile(
    r"(^|[\s;&|])(sudo|curl|wget|npx)([\s;&|]|$)"
    r"|(^|[\s;&|])(apt|apt-get)\s+(-[^\s;&|]+\s+)*"
    r"(install|remove|purge|update|upgrade|dist-upgrade|full-upgrade|"
    r"autoremove|download|source|build-dep|satisfy)([\s;&|]|$)"
    r"|(^|[\s;&|])(pip3?|python3?\s+-m\s+pip)\s+install([\s;&|]|$)"
    r"|(^|[\s;&|])npm\s+(install|i|add|ci|update|exec)([\s;&|]|$)"
    r"|(^|[\s;&|])yarn\s+(-[^\s;&|]+\s+)*"
    r"(add|install|upgrade|remove|dlx|global)([\s;&|]|$)"
    r"|(^|[\s;&|])git\s+(clone|fetch|pull|remote)([\s;&|]|$)",
    re.IGNORECASE,
)


def validate(
    recreation: Path,
    *,
    blocked_log: Path | None = None,
    reject_literal_sudo: bool = False,
) -> list[str]:
    build_script = recreation / "build.sh"
    launch_script = recreation / "launch.sh"
    if not build_script.exists():
        return ["recreation/build.sh missing"]

    content = build_script.read_text(errors="replace")
    checked_content = "\n".join(
        line for line in content.splitlines() if not line.lstrip().startswith("#")
    )
    errors = []
    source = recreation / "src"
    source_count = sum(len(files) for _root, _dirs, files in os.walk(source))
    if source_count <= 0:
        errors.append("src/ is empty")
    if not launch_script.exists():
        errors.append("launch.sh missing")
    if blocked_log and blocked_log.exists() and blocked_log.stat().st_size > 0:
        errors.append("blocked command log is non-empty")
    if FORBIDDEN_BUILD_COMMAND.search(checked_content):
        errors.append("build.sh contains forbidden install/network command")
    if reject_literal_sudo and "sudo " in content:
        errors.append("build.sh contains literal sudo")
    if "/workspace/install" in content:
        errors.append("build.sh references /workspace/install")
    if subprocess.run(
        ["bash", "-n", str(build_script)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=False,
    ).returncode:
        errors.append("build.sh bash syntax invalid")
    if (
        launch_script.exists()
        and subprocess.run(
            ["bash", "-n", str(launch_script)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
    ):
        errors.append("launch.sh bash syntax invalid")
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("recreation", type=Path)
    parser.add_argument("--blocked-log", type=Path)
    parser.add_argument("--reject-literal-sudo", action="store_true")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()
    errors = validate(
        args.recreation,
        blocked_log=args.blocked_log,
        reject_literal_sudo=args.reject_literal_sudo,
    )
    if errors:
        if not args.quiet:
            if errors == ["recreation/build.sh missing"]:
                print("ERROR: recreation/build.sh missing")
            else:
                print("ERROR: recreation contract failed: " + "; ".join(errors))
        return 1
    if not args.quiet:
        print("Recreation contract verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
