#!/usr/bin/env python3
"""Small executable helpers used by the Android lifecycle shell.

The shell owns process ordering and device commands. Structured-data handling
and Python imports live here so the release path contains no embedded programs.
"""

from __future__ import annotations

import argparse
import importlib
import json
import re
import sys
from pathlib import Path

REQUIRED_MOBILE_TOOLS = frozenset(
    {
        "mobile_list_available_devices",
        "mobile_launch_app",
        "mobile_ui_dump",
        "mobile_take_screenshot",
        "mobile_click_on_screen_at_coordinates",
        "mobile_type_keys",
        "mobile_press_button",
        "mobile_swipe_on_screen",
    }
)

DATA_URI = re.compile(r"data:image/[^;]+;base64,[A-Za-z0-9+/=]{100,}")
ANTHROPIC_IMAGE_DATA = re.compile(r'("data"\s*:\s*")[A-Za-z0-9+/=]{200,}(")')


def render_prompt(stage: str, device_id: str) -> str:
    if stage != "recreation":
        raise ValueError(f"unsupported prompt stage: {stage}")
    from core import recreation_prompt

    return recreation_prompt.render("android", device_id=device_id)


def trajectory_summary(path: Path) -> str:
    texts: list[str] = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            event = json.loads(line)
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(event, dict) and event.get("type") == "result":
            texts.append(str(event.get("result", ""))[:1200])
    lines = ["probe agent said:"]
    if texts:
        lines.extend(f"    {line}" for line in texts[-1].splitlines())
    return "\n".join(lines)


def mcp_config(binary: str, android_home: str, path: str, coordinate_space: str) -> dict:
    from core import mcp_settings

    return mcp_settings.mcp_config(
        mcp_settings.MOBILE_SERVER,
        binary,
        [],
        {
            "ANDROID_HOME": android_home,
            "PATH": path,
            "MOBILE_MCP_COORDINATE_SPACE": coordinate_space,
        },
    )


def validate_mobile_tools(lines: list[str]) -> list[dict]:
    tools = None
    for line in lines:
        try:
            message = json.loads(line.strip())
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if isinstance(message, dict) and message.get("id") == 2:
            result = message.get("result")
            if isinstance(result, dict):
                tools = result.get("tools")
                break
    if not isinstance(tools, list):
        raise ValueError("no tools/list result found in mobile-mcp response")
    names = {tool.get("name") for tool in tools if isinstance(tool, dict)}
    missing = sorted(REQUIRED_MOBILE_TOOLS - names)
    if missing:
        raise ValueError("pinned mobile-mcp is missing required tools: " + ", ".join(missing))
    return tools


def format_mobile_tools(tools: list[dict]) -> str:
    lines = [f"    mobile-mcp exposes {len(tools)} tools:"]
    for tool in tools:
        name = tool.get("name", "?")
        description = tool.get("description") or ""
        first_line = description.splitlines()[0] if description else ""
        lines.append(f"    - {name}: {first_line}")
        schema = tool.get("inputSchema") or tool.get("input_schema")
        if schema:
            lines.extend(
                "        " + line
                for line in json.dumps(schema, ensure_ascii=False, indent=2).splitlines()
            )
    return "\n".join(lines)


def package_metadata(root: Path, package: str) -> tuple[Path, str]:
    package_path = root / "node_modules" / package / "package.json"
    data = json.loads(package_path.read_text())
    bins = data.get("bin") or {}
    if isinstance(bins, str):
        binary = bins
    elif isinstance(bins, dict):
        binary = bins.get("mobile-mcp") or next(iter(bins.values()), "")
    else:
        binary = ""
    if not binary:
        raise ValueError("published package has no executable")
    return (package_path.parent / binary).resolve(), str(data.get("version") or "")


def check_eval_dependencies() -> None:
    for module in ("pytest", "PIL.Image"):
        importlib.import_module(module)


def filter_base64(lines) -> None:
    for line in lines:
        line = DATA_URI.sub("[BASE64_IMG]", line)
        line = ANTHROPIC_IMAGE_DATA.sub(r"\1[BASE64_IMG]\2", line)
        sys.stdout.write(line)
        sys.stdout.flush()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="android.runtime_helpers")
    commands = parser.add_subparsers(dest="command", required=True)

    prompt = commands.add_parser("render-prompt")
    prompt.add_argument("--stage", default="recreation")
    prompt.add_argument("--device-id", required=True)

    summary = commands.add_parser("trajectory-summary")
    summary.add_argument("path", type=Path)

    config = commands.add_parser("mcp-config")
    config.add_argument("--binary", required=True)
    config.add_argument("--android-home", required=True)
    config.add_argument("--path", required=True)
    config.add_argument("--coordinate-space", required=True)

    commands.add_parser("validate-mobile-tools")

    command_json = commands.add_parser("command-json")
    command_json.add_argument("value")

    metadata = commands.add_parser("package-metadata")
    metadata.add_argument("--root", required=True, type=Path)
    metadata.add_argument("--package", required=True)
    metadata.add_argument("--field", required=True, choices=("binary", "version"))

    descriptor = commands.add_parser("descriptor-package")
    descriptor.add_argument("path", type=Path)

    commands.add_parser("check-eval-dependencies")
    commands.add_parser("filter-base64")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "render-prompt":
        print(render_prompt(args.stage, args.device_id), end="")
    elif args.command == "trajectory-summary":
        print(trajectory_summary(args.path))
    elif args.command == "mcp-config":
        print(
            json.dumps(mcp_config(args.binary, args.android_home, args.path, args.coordinate_space))
        )
    elif args.command == "validate-mobile-tools":
        print(format_mobile_tools(validate_mobile_tools(list(sys.stdin))))
    elif args.command == "command-json":
        print(json.dumps([args.value]))
    elif args.command == "package-metadata":
        binary, version = package_metadata(args.root, args.package)
        print(binary if args.field == "binary" else version, end="")
    elif args.command == "descriptor-package":
        print(json.loads(args.path.read_text()).get("package") or "", end="")
    elif args.command == "check-eval-dependencies":
        check_eval_dependencies()
    elif args.command == "filter-base64":
        filter_base64(sys.stdin)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
