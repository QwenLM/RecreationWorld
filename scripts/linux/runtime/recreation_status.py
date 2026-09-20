#!/usr/bin/env python3
"""Inspect Linux recreation output for resume and final verification."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path

FORBIDDEN_COMMAND = re.compile(
    r"(^|[\s;&|])(sudo|curl|wget|npx)([\s;&|]|$)"
    r"|(^|[\s;&|])(apt|apt-get)\s+(-[^\s;&|]+\s+)*"
    r"(install|remove|purge|update|upgrade|dist-upgrade|full-upgrade|autoremove|download|source|build-dep|satisfy)([\s;&|]|$)"
    r"|(^|[\s;&|])(pip3?|python3?\s+-m\s+pip)\s+install([\s;&|]|$)"
    r"|(^|[\s;&|])npm\s+(install|i|add|ci|update|exec)([\s;&|]|$)"
    r"|(^|[\s;&|])yarn\s+(-[^\s;&|]+\s+)*(add|install|upgrade|remove|dlx|global)([\s;&|]|$)"
    r"|(^|[\s;&|])git\s+(clone|fetch|pull|remote)([\s;&|]|$)",
    re.I,
)


def _valid_shell(path: Path) -> bool:
    return (
        subprocess.run(
            ["bash", "-n", str(path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        ).returncode
        == 0
    )


def inspect_recreation(output_dir: Path) -> dict[str, object]:
    recreation = output_dir / "recreation"
    build_sh = recreation / "build.sh"
    launch_sh = recreation / "launch.sh"
    source_dir = recreation / "src"
    source_count = sum(len(files) for _, _, files in os.walk(source_dir))
    result: dict[str, object] = {
        "has_build": build_sh.exists(),
        "has_launch": launch_sh.exists(),
        "src_count": source_count,
        "bash_syntax": False,
        "launch_syntax": False,
        "forbidden_command": False,
        "references_workspace_reference": False,
        "blocked_commands": False,
    }
    if build_sh.exists():
        content = build_sh.read_text(errors="replace")
        checked_content = "\n".join(
            line for line in content.splitlines() if not line.lstrip().startswith("#")
        )
        result["forbidden_command"] = bool(FORBIDDEN_COMMAND.search(checked_content))
        result["references_workspace_reference"] = "/workspace/reference" in content
        result["bash_syntax"] = _valid_shell(build_sh)
    if launch_sh.exists():
        result["launch_syntax"] = _valid_shell(launch_sh)
    blocked_log = output_dir / "blocked_commands.log"
    result["blocked_commands"] = blocked_log.exists() and blocked_log.stat().st_size > 0
    result["ok"] = bool(
        result["has_build"]
        and result["has_launch"]
        and source_count > 0
        and result["bash_syntax"]
        and result["launch_syntax"]
        and not result["forbidden_command"]
        and not result["references_workspace_reference"]
        and not result["blocked_commands"]
    )
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("complete", "verify"))
    parser.add_argument("output_dir", type=Path)
    args = parser.parse_args()

    result = inspect_recreation(args.output_dir)
    if args.command == "complete":
        if not result["has_build"]:
            print("PENDING")
        else:
            print("DONE" if result["ok"] else "REDO")
    else:
        print(json.dumps(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
