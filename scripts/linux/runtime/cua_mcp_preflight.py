#!/usr/bin/env python3
"""Validate that the Linux desktop MCP sees the intended application window."""

from __future__ import annotations

import base64
import json
import os
import select
import shlex
import subprocess
import sys
import tempfile
import time
from pathlib import Path

LABEL = os.environ.get("RB_PREFLIGHT_LABEL", "desktop-control")
PID_HINT = os.environ.get("RB_PREFLIGHT_PID", "").strip()
MCP_COMMAND = shlex.split(
    os.environ.get("RB_CUA_MCP_COMMAND", "cua-driver mcp --no-overlay")
)


def as_int(value, default=0):
    try:
        return int(value)
    except Exception:
        return default


def walk(value):
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from walk(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk(child)


def color_count_file(path):
    path = Path(path)
    if not path.exists() or path.stat().st_size <= 0:
        return 0
    proc = subprocess.run(
        ["identify", "-format", "%k", str(path)],
        text=True,
        capture_output=True,
        timeout=10,
    )
    if proc.returncode != 0:
        return 0
    return as_int(proc.stdout.strip(), 0)


def color_count_png(data):
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    try:
        return color_count_file(tmp_path)
    finally:
        Path(tmp_path).unlink(missing_ok=True)


def inspect_payload(value):
    max_elements = 0
    tree_preview = ""
    image_colors = []
    parsed_strings = set()

    def inspect(value):
        nonlocal max_elements, tree_preview
        for item in walk(value):
            if isinstance(item, dict):
                if isinstance(item.get("element_count"), int):
                    max_elements = max(max_elements, item["element_count"])
                if isinstance(item.get("tree_markdown"), str) and not tree_preview:
                    tree_preview = " ".join(item["tree_markdown"].split())[:160]

                if item.get("type") == "image" and item.get("data"):
                    try:
                        image_colors.append(
                            color_count_png(base64.b64decode(item["data"]))
                        )
                    except Exception:
                        pass

                source = item.get("source")
                if isinstance(source, dict) and source.get("data"):
                    media_type = source.get("media_type") or source.get("mimeType")
                    if media_type in ("image/png", "image/jpeg", "image/jpg"):
                        try:
                            image_colors.append(
                                color_count_png(base64.b64decode(source["data"]))
                            )
                        except Exception:
                            pass

                if item.get("mimeType") in (
                    "image/png",
                    "image/jpeg",
                    "image/jpg",
                ) and item.get("data"):
                    try:
                        image_colors.append(
                            color_count_png(base64.b64decode(item["data"]))
                        )
                    except Exception:
                        pass

                if item.get("type") == "text" and isinstance(item.get("text"), str):
                    text = item["text"].strip()
                    if text[:1] in ("{", "[") and text not in parsed_strings:
                        parsed_strings.add(text)
                        try:
                            inspect(json.loads(text))
                        except Exception:
                            pass
            elif isinstance(item, str):
                text = item.strip()
                if (
                    text[:1] in ("{", "[")
                    and text not in parsed_strings
                    and (
                        "tree_markdown" in text
                        or "element_count" in text
                        or "image" in text
                    )
                ):
                    parsed_strings.add(text)
                    try:
                        inspect(json.loads(text))
                    except Exception:
                        pass

    inspect(value)
    return max_elements, (max(image_colors) if image_colors else 0), tree_preview


def run_cli_tool(name, payload=None):
    cmd = ["cua-driver", "call", name]
    if payload is not None:
        cmd.append(json.dumps(payload, separators=(",", ":")))
    proc = subprocess.run(cmd, text=True, capture_output=True, timeout=25)
    if proc.returncode != 0:
        raise RuntimeError(
            f"{name} failed: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return json.loads(proc.stdout or "{}")


def process_family(pid_hint):
    root = as_int(pid_hint)
    if root <= 0:
        return set()
    seen = {root}
    pending = [root]
    while pending:
        parent = pending.pop()
        try:
            children = (
                Path(f"/proc/{parent}/task/{parent}/children").read_text().split()
            )
        except OSError:
            children = []
        for raw in children:
            child = as_int(raw)
            if child > 0 and child not in seen:
                seen.add(child)
                pending.append(child)
    return seen


def list_windows_for_cli(pid_hint):
    allowed_pids = process_family(pid_hint)
    if not allowed_pids:
        raise RuntimeError(f"reference PID is not alive: {pid_hint!r}")
    # Some GUI frameworks put the window on a child process, so query each member of the
    # reference process tree.  Never fall back to an arbitrary desktop window: that made a
    # terminal/desktop root with one AX node certify an unrelated reference application.
    attempts = [{"pid": pid, "on_screen_only": True} for pid in sorted(allowed_pids)]
    attempts.append({"on_screen_only": True})

    for payload in attempts:
        try:
            listed = run_cli_tool("list_windows", payload)
        except Exception:
            continue
        windows = listed.get("windows", [])
        visible = []
        for window in windows:
            width = as_int(window.get("width"))
            height = as_int(window.get("height"))
            if (
                as_int(window.get("pid")) in allowed_pids
                and width >= 50
                and height >= 50
            ):
                visible.append(window)
        if visible:
            return sorted(
                visible,
                key=lambda w: as_int(w.get("width")) * as_int(w.get("height")),
                reverse=True,
            )
    return []


class McpClient:
    def __init__(self):
        self.proc = subprocess.Popen(
            MCP_COMMAND,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        self.next_id = 1

    def close(self):
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()

    def request(self, method, params=None, timeout=25):
        req_id = self.next_id
        self.next_id += 1
        msg = {"jsonrpc": "2.0", "id": req_id, "method": method}
        if params is not None:
            msg["params"] = params
        self.proc.stdin.write(json.dumps(msg, separators=(",", ":")) + "\n")
        self.proc.stdin.flush()
        return self._read_response(req_id, method, timeout)

    def notify(self, method, params=None):
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self.proc.stdin.write(json.dumps(msg, separators=(",", ":")) + "\n")
        self.proc.stdin.flush()

    def _read_response(self, req_id, method, timeout):
        fd = self.proc.stdout.fileno()
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.proc.poll() is not None:
                break
            ready, _, _ = select.select([fd], [], [], 0.2)
            if not ready:
                continue
            line = self.proc.stdout.readline()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if msg.get("id") == req_id:
                if "error" in msg:
                    raise RuntimeError(f"MCP {req_id} error: {msg['error']}")
                return msg.get("result", {})
        stderr = ""
        try:
            err_fd = self.proc.stderr.fileno()
            ready, _, _ = select.select([err_fd], [], [], 0)
            if ready:
                stderr = os.read(err_fd, 2000).decode("utf-8", "replace")
        except Exception:
            pass
        raise RuntimeError(
            f"MCP request {method!r} timed out or exited. stderr={stderr!r}"
        )

    def call_tool(self, name, arguments):
        result = self.request("tools/call", {"name": name, "arguments": arguments})
        if result.get("isError"):
            raise RuntimeError(f"MCP tool {name} returned error: {result}")
        return result


def run_mcp_window_state(payload):
    client = McpClient()
    try:
        client.request(
            "initialize",
            {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "rb-cua-preflight", "version": "1"},
            },
        )
        client.notify("notifications/initialized", {})
        result = client.call_tool("get_window_state", payload)
        return inspect_payload(result)
    finally:
        client.close()


last_error = ""
for attempt in range(1, 16):
    try:
        windows = list_windows_for_cli(PID_HINT)
        if not windows:
            raise RuntimeError("no visible windows")

        window = windows[0]
        window_ref = {
            "pid": as_int(window["pid"]),
            "window_id": as_int(window["window_id"]),
        }
        element_count, _, tree_preview = run_mcp_window_state(
            {**window_ref, "capture_mode": "ax"}
        )
        _, image_color_count, _ = run_mcp_window_state(
            {**window_ref, "capture_mode": "screenshot"}
        )

        if image_color_count < 2:
            raise RuntimeError(
                f"MCP screenshot has too few colors ({image_color_count}): "
                f"elements={element_count} tree={tree_preview!r}"
            )

        if element_count < 3:
            raise RuntimeError(
                f"AT-SPI tree is not usable ({element_count} elements): tree={tree_preview!r}"
            )

        win_w = as_int(window.get("width"))
        win_h = as_int(window.get("height"))
        if win_w < 200 or win_h < 50:
            raise RuntimeError(
                f"Window too small ({win_w}x{win_h}): expected at least 200x50"
            )

        print(
            "CUA MCP preflight: PASS "
            f"label={LABEL!r} window={window.get('title')!r} pid={window.get('pid')} "
            f"size={win_w}x{win_h} "
            f"image_colors={image_color_count} elements={element_count}"
        )
        sys.exit(0)
    except Exception as exc:
        last_error = str(exc)
        print(
            f"CUA MCP preflight: attempt {attempt}/15 failed: {last_error}",
            file=sys.stderr,
        )
        time.sleep(3)

print(f"ERROR: CUA MCP preflight failed for {LABEL}: {last_error}", file=sys.stderr)
sys.exit(1)
