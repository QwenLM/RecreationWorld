#!/usr/bin/env python3
"""Probe a running qwen-cua-driver daemon's coordinate contract."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess

BIN = os.environ["CUA_BIN"]
SOCK = os.environ["SOCK"]


def rpc(method: str, params: dict, timeout: int = 20) -> dict | None:
    command = [BIN, "mcp", "--socket", SOCK]
    initialize = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "chk", "version": "1"},
            },
        }
    )
    initialized = json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"})
    call = json.dumps({"jsonrpc": "2.0", "id": 2, "method": method, "params": params})
    process = None
    try:
        # Remove coordinate variables from the probe process so the daemon is
        # the only component that can normalize the protocol.
        environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("CUA_DRIVER_RS_COORDINATE")
        }
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            preexec_fn=os.setsid,
            env=environment,
        )
        output, _ = process.communicate(
            input=f"{initialize}\n{initialized}\n{call}\n",
            timeout=timeout,
        )
    except Exception:
        if process is not None:
            try:
                os.killpg(os.getpgid(process.pid), signal.SIGKILL)
            except Exception:
                pass
        return None
    for line in output.splitlines():
        try:
            message = json.loads(line.strip())
        except Exception:
            continue
        if message.get("id") == 2:
            return message
    return None


def main() -> None:
    tools = rpc("tools/list", {})
    schema = json.dumps(tools, ensure_ascii=False) if tools else ""
    if any(
        marker in schema
        for marker in ("normalized to", "0–1000 normalized", "0-1000 normalized")
    ):
        print(
            "[coord] (b) OK: tools/list coord fields read "
            "'...0-1000 normalized to...' -> RELATIVE mode ACTIVE in daemon"
        )
    else:
        print(
            "[coord] (b) FAIL: tools/list coord fields NOT normalized -> "
            "relative mode NOT active (check COORDINATE_SPACE on daemon)"
        )

    desktop = rpc("tools/call", {"name": "get_desktop_state", "arguments": {}})
    if not desktop:
        return
    match = re.search(r'"screenshot_width"\s*:\s*(\d+)', json.dumps(desktop))
    if not match:
        return
    width = int(match.group(1))
    note = (
        "(expected: absolute pixels in 0.7.3)"
        if width != 1000
        else "(WARN: 1000 means OLD pre-0.7.1 driver!)"
    )
    print(f"[coord] (info) get_desktop_state screenshot_width={width} {note}")


if __name__ == "__main__":
    main()
