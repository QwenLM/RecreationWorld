#!/usr/bin/env python3
"""Attach Playwright MCP to a live capture browser, with safe fallback."""

from __future__ import annotations

import os
import socket
import sys
from urllib.parse import urlparse


def _reachable(endpoint: str) -> bool:
    parsed = urlparse(endpoint)
    try:
        with socket.create_connection(
            (parsed.hostname or "127.0.0.1", parsed.port or 80), timeout=1
        ):
            return True
    except OSError:
        return False


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        print("usage: playwright_mcp_capture.py MCP CDP [ARGS...]", file=sys.stderr)
        return 2
    command, endpoint, *args = argv[1:]
    if _reachable(endpoint):
        clean: list[str] = []
        skip = False
        for arg in args:
            if skip:
                skip = False
                continue
            if arg == "--executable-path":
                skip = True
                continue
            if arg != "--headless":
                clean.append(arg)
        args = [*clean, "--cdp-endpoint", endpoint]
    os.execvp(command, [command, *args])
    return 127  # pragma: no cover


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
