#!/usr/bin/env python3
"""Read small runtime metadata values for Linux shell stages."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from urllib.parse import urlparse


def executable(metadata_path: Path, require_existing: bool) -> None:
    try:
        value = str(json.loads(metadata_path.read_text()).get("executable", ""))
    except (OSError, AttributeError, json.JSONDecodeError):
        value = ""
    if require_existing and (not value or not Path(value).is_file()):
        value = ""
    print(value)


def endpoint(url: str) -> None:
    parsed = urlparse(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    print(f"{parsed.hostname or ''},{port}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    executable_parser = commands.add_parser("executable")
    executable_parser.add_argument("metadata", type=Path)
    executable_parser.add_argument("--require-existing", action="store_true")
    endpoint_parser = commands.add_parser("endpoint")
    endpoint_parser.add_argument("url")
    args = parser.parse_args()

    if args.command == "executable":
        executable(args.metadata, args.require_existing)
    else:
        endpoint(args.url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
