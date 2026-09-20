#!/usr/bin/env python3
"""Strict stdio MCP preflight probes used immediately before recreation agents.

The basic health check (initialize + tools/list) is intentionally retained for
callers that only need catalogue validation. Release recreation paths use one
of the functional probes below: they call the same stdio server as the agent and
prove that it can observe the live reference application.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import queue
import re
import subprocess
import threading
import time
from collections.abc import Callable, Iterable
from typing import Any


class McpError(RuntimeError):
    """The MCP bridge exited, timed out, or returned an error response."""


class McpClient:
    """Small line-delimited stdio MCP client that also works on Windows pipes.

    ``select`` cannot wait on a subprocess pipe on Windows. Reader threads keep
    the protocol portable and, unlike ``communicate()``, allow dependent calls
    (navigate then snapshot, launch then dump) in one server session.
    """

    def __init__(
        self,
        command: list[str],
        *,
        env: dict[str, str] | None = None,
        timeout: int = 30,
    ) -> None:
        self.timeout = timeout
        self.proc = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            # MCP framing is UTF-8 by spec, but ``text=True`` alone decodes with the
            # locale codec, which is cp1252/cp936 on the Windows VMs.  A single
            # non-ASCII byte in a window title then raised UnicodeDecodeError inside
            # the reader thread below; the thread died, every later response was lost,
            # and the only visible symptom was a preflight timeout.  ``errors=replace``
            # keeps a bad byte local to its own line, where the existing
            # JSONDecodeError branch already drops just that line.
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            env=env,
        )
        self._responses: queue.Queue[dict[str, Any]] = queue.Queue()
        self._stderr: list[str] = []
        self._skipped_lines = 0
        self._reader_error = ""
        self._stdout_eof = False
        self._next_id = 1
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    def _read_stdout(self) -> None:
        assert self.proc.stdout is not None
        # Both readers record why they stopped instead of letting the daemon thread die
        # unobserved.  Without this the caller could only report "timed out", which is
        # indistinguishable from a genuinely unresponsive MCP server.
        try:
            for line in self.proc.stdout:
                try:
                    message = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    self._skipped_lines += 1
                    continue
                if isinstance(message, dict):
                    self._responses.put(message)
        except Exception as exc:
            self._reader_error = f"stdout reader stopped: {exc!r}"
        finally:
            # EOF is the other, quieter way this reader stops: a server that closes stdout
            # without exiting raises nothing for the branch above to catch, so the caller could
            # only report an ordinary timeout -- the same misdiagnosis the decode fix removed.
            # Set after the last put, so observing it means every response read is queued.
            self._stdout_eof = True

    def _read_stderr(self) -> None:
        assert self.proc.stderr is not None
        try:
            for line in self.proc.stderr:
                self._stderr.append(line.rstrip())
                if len(self._stderr) > 30:
                    del self._stderr[:-30]
        except Exception as exc:
            self._stderr.append(f"[stderr reader stopped: {exc!r}]")

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait(timeout=2)

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._write(message)

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: int | None = None,
    ) -> dict[str, Any]:
        request_id = self._next_id
        self._next_id += 1
        message: dict[str, Any] = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
        }
        if params is not None:
            message["params"] = params
        self._write(message)

        deadline = time.monotonic() + (timeout or self.timeout)
        deferred: list[dict[str, Any]] = []
        try:
            while time.monotonic() < deadline:
                # Nothing further can arrive once the process is gone or its stdout is at EOF,
                # so waiting out the deadline only delays the diagnosis -- three attempts of 30s
                # each spent 90s to learn what was already known. Both flags are set after the
                # reader's last put, which is what makes an empty queue conclusive here.
                if (
                    self.proc.poll() is not None or self._stdout_eof
                ) and self._responses.empty():
                    break
                try:
                    response = self._responses.get(
                        timeout=max(0.05, min(0.25, deadline - time.monotonic()))
                    )
                except queue.Empty:
                    continue
                if response.get("id") != request_id:
                    deferred.append(response)
                    continue
                if "error" in response:
                    raise McpError(f"{method} returned error: {response['error']}")
                result = response.get("result")
                return result if isinstance(result, dict) else {"value": result}
        finally:
            for response in deferred:
                self._responses.put(response)

        stderr = " | ".join(self._stderr[-5:])[:600]
        rc = self.proc.poll()
        diagnosis = f"rc={rc}, stderr={stderr!r}"
        if self._stdout_eof and rc is None:
            # The one combination a plain timeout hides: the bridge is still running, but it
            # closed the channel, so it was never going to answer.
            diagnosis += ", stdout closed while MCP still running"
        elif self._stdout_eof:
            diagnosis += ", stdout at EOF"
        if self._skipped_lines:
            diagnosis += f", non_json_lines={self._skipped_lines}"
        if self._reader_error:
            diagnosis += f", {self._reader_error}"
        raise McpError(f"{method} timed out or MCP exited ({diagnosis})")

    def _write(self, message: dict[str, Any]) -> None:
        if self.proc.poll() is not None:
            raise McpError(f"MCP exited before request (rc={self.proc.returncode})")
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            self.proc.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise McpError(f"could not write MCP request: {exc}") from exc

    def initialize(self) -> list[dict[str, Any]]:
        self.request(
            "initialize",
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "rb-mcp-preflight", "version": "2"},
            },
        )
        self.notify("notifications/initialized", {})
        result = self.request("tools/list", {})
        tools = result.get("tools")
        if not isinstance(tools, list) or not tools:
            raise McpError("empty tools/list")
        return [tool for tool in tools if isinstance(tool, dict)]

    def call_tool(
        self, name: str, arguments: dict[str, Any], *, timeout: int | None = None
    ) -> dict[str, Any]:
        result = self.request(
            "tools/call",
            {"name": name, "arguments": arguments},
            timeout=timeout,
        )
        if result.get("isError"):
            raise McpError(f"{name} returned an error: {_text_content(result)[:500]}")
        return result


def tools_list(command: list[str], timeout: int = 30) -> tuple[bool, str]:
    """Run initialize + tools/list and require a non-empty tool catalogue.

    This communicate-based compatibility path is kept for old health-check
    callers. Functional release probes below use :class:`McpClient`.
    """
    requests = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "rb-mcp-preflight", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    proc = subprocess.Popen(
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        # Same locale-codec hazard as McpClient; here it would surface as
        # communicate() raising instead of returning "no tools/list response".
        encoding="utf-8",
        errors="replace",
    )
    try:
        stdout, stderr = proc.communicate(
            "".join(
                json.dumps(request, separators=(",", ":")) + "\n"
                for request in requests
            ),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()
        return False, "tools/list timeout"

    for line in stdout.splitlines():
        try:
            response = json.loads(line)
        except json.JSONDecodeError:
            continue
        if response.get("id") != 2:
            continue
        tools = response.get("result", {}).get("tools", [])
        if tools:
            return True, f"tools={len(tools)}"
        return False, "empty tools/list"
    return False, "no tools/list response: " + (stderr or "")[:300]


def _walk_decoded(value: Any, seen: set[str] | None = None) -> Iterable[Any]:
    """Walk JSON values, including JSON serialized inside MCP text blocks."""
    if seen is None:
        seen = set()
    yield value
    if isinstance(value, dict):
        for child in value.values():
            yield from _walk_decoded(child, seen)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_decoded(child, seen)
    elif isinstance(value, str):
        text = value.strip()
        if text[:1] in ("{", "[") and text not in seen:
            seen.add(text)
            try:
                decoded = json.loads(text)
            except json.JSONDecodeError:
                return
            yield from _walk_decoded(decoded, seen)


def _text_content(value: Any) -> str:
    texts: list[str] = []
    for item in _walk_decoded(value):
        if isinstance(item, dict) and item.get("type") == "text":
            text = item.get("text")
            if isinstance(text, str):
                texts.append(text)
    return "\n".join(texts)


def _tool_names(tools: list[dict[str, Any]]) -> set[str]:
    return {str(tool.get("name")) for tool in tools if tool.get("name")}


def _require_tools(tools: list[dict[str, Any]], required: set[str]) -> None:
    missing = sorted(required - _tool_names(tools))
    if missing:
        raise McpError("required MCP tools are missing: " + ", ".join(missing))


def _as_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _windows(value: Any) -> list[dict[str, Any]]:
    for item in _walk_decoded(value):
        if not isinstance(item, dict) or not isinstance(item.get("windows"), list):
            continue
        return [window for window in item["windows"] if isinstance(window, dict)]
    return []


def _window_dimension(window: dict[str, Any], name: str) -> int:
    """Read dimensions from both legacy flat and canonical nested window records."""
    if name in window:
        return _as_int(window.get(name))
    bounds = window.get("bounds")
    if isinstance(bounds, dict):
        return _as_int(bounds.get(name))
    return 0


def _element_count(value: Any) -> int:
    counts: list[int] = []
    for item in _walk_decoded(value):
        if isinstance(item, dict):
            if "element_count" in item:
                counts.append(_as_int(item["element_count"]))
            tree = item.get("tree_markdown")
            if isinstance(tree, str) and tree.strip():
                counts.append(len([line for line in tree.splitlines() if line.strip()]))
    return max(counts, default=0)


def _image_bytes(value: Any) -> int:
    sizes: list[int] = []
    for item in _walk_decoded(value):
        if not isinstance(item, dict):
            continue
        candidates: list[Any] = []
        if item.get("type") == "image":
            candidates.append(item.get("data"))
        source = item.get("source")
        if isinstance(source, dict) and str(source.get("media_type", "")).startswith(
            "image/"
        ):
            candidates.append(source.get("data"))
        if str(item.get("mimeType", "")).startswith("image/"):
            candidates.append(item.get("data"))
        for encoded in candidates:
            if not isinstance(encoded, str) or not encoded:
                continue
            try:
                sizes.append(len(base64.b64decode(encoded, validate=True)))
            except (ValueError, TypeError):
                continue
    return max(sizes, default=0)


def _process_family(pid: int) -> set[int]:
    """Best-effort descendants for browser/framework multi-process windows."""
    family = {pid}
    if os.name == "nt":
        try:
            proc = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-Command",
                    "Get-CimInstance Win32_Process | "
                    "Select-Object ProcessId,ParentProcessId | "
                    "ConvertTo-Json -Compress",
                ],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=10,
                check=False,
            )
            rows = json.loads(proc.stdout.lstrip("\ufeff") or "[]")
            if isinstance(rows, dict):
                rows = [rows]
            children: dict[int, list[int]] = {}
            for row in rows:
                child = _as_int(row.get("ProcessId"))
                parent = _as_int(row.get("ParentProcessId"))
                if child > 0:
                    children.setdefault(parent, []).append(child)
            pending = [pid]
            while pending:
                for child in children.get(pending.pop(), []):
                    if child not in family:
                        family.add(child)
                        pending.append(child)
        except (OSError, subprocess.SubprocessError, ValueError, TypeError):
            pass
        return family
    try:
        proc = subprocess.run(
            ["ps", "-eo", "pid=,ppid="],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return family
    children: dict[int, list[int]] = {}
    for line in proc.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        child, parent = map(_as_int, fields)
        children.setdefault(parent, []).append(child)
    pending = [pid]
    while pending:
        for child in children.get(pending.pop(), []):
            if child > 0 and child not in family:
                family.add(child)
                pending.append(child)
    return family


ClientFactory = Callable[..., McpClient]


def desktop_reference(
    command: list[str],
    pid: int,
    *,
    timeout: int = 30,
    attempts: int = 3,
    include_descendants: bool = False,
    allow_windowless: bool = False,
    env: dict[str, str] | None = None,
    client_factory: ClientFactory = McpClient,
) -> tuple[bool, str]:
    """Require a real target window, usable AX/UIA tree, and screenshot.

    ``allow_windowless`` soft-passes a reference that owns no top-level window once
    the MCP round-trip itself is verified. macOS menu-bar-only apps (LSUIElement,
    e.g. clarkio/WhoDis) legitimately have no window; the private mainline preflight
    never required one, so callers on that platform set this to stay aligned. Windows
    and Linux leave it False and keep the strict window requirement.
    """
    if pid <= 0:
        return False, "reference PID is required"
    last_error = ""
    for attempt in range(1, attempts + 1):
        client = client_factory(command, env=env, timeout=timeout)
        try:
            tools = client.initialize()
            _require_tools(tools, {"list_windows", "get_window_state"})
            allowed = _process_family(pid) if include_descendants else {pid}
            # Chromium/Electron may own its top-level window in a child process.
            # An unfiltered list is safe here because every returned window is
            # subsequently constrained to the captured process family.
            # Do not ask WindowServer for ``on_screen_only`` here. On the
            # remote macOS console session, kCGWindowListOptionOnScreenOnly can
            # return an empty set even while the same daemon can enumerate and
            # capture those windows through the unfiltered path. The checks
            # below still require a target-owned, non-trivial window, a usable
            # accessibility tree, and real screenshot bytes.
            list_args = {} if include_descendants else {"pid": pid}
            listed_windows = _windows(client.call_tool("list_windows", list_args))
            visible = [
                window
                for window in listed_windows
                if _as_int(window.get("pid")) in allowed
                and _window_dimension(window, "width") >= 50
                and _window_dimension(window, "height") >= 50
            ]
            if not visible:
                observed = [
                    {
                        "pid": _as_int(window.get("pid")),
                        "window_id": _as_int(window.get("window_id")),
                        "title": str(window.get("title", ""))[:80],
                    }
                    for window in listed_windows
                    if _window_dimension(window, "width") >= 50
                    and _window_dimension(window, "height") >= 50
                ]
                if allow_windowless:
                    return (
                        True,
                        f"pid={pid} windowless (menu-bar/LSUIElement) reference; MCP verified, "
                        f"no top-level window (observed={observed[:5]})",
                    )
                raise McpError(
                    f"no visible window owned by reference PID family {sorted(allowed)}; "
                    f"observed={observed[:10]}"
                )
            window = max(
                visible,
                key=lambda item: _window_dimension(item, "width")
                * _window_dimension(item, "height"),
            )
            if _window_dimension(window, "width") < 200:
                raise McpError(
                    "reference window is too small "
                    f"({_window_dimension(window, 'width')}x"
                    f"{_window_dimension(window, 'height')})"
                )
            window_ref = {
                "pid": _as_int(window.get("pid")),
                "window_id": _as_int(window.get("window_id")),
            }
            ax = client.call_tool(
                "get_window_state", {**window_ref, "capture_mode": "ax"}
            )
            count = _element_count(ax)
            if count < 3:
                raise McpError(
                    f"accessibility tree is unusable (element_count={count})"
                )
            screenshot = client.call_tool(
                "get_window_state", {**window_ref, "capture_mode": "screenshot"}
            )
            image_size = _image_bytes(screenshot)
            if image_size < 128:
                raise McpError(
                    f"reference screenshot is empty/invalid ({image_size} bytes)"
                )
            return (
                True,
                f"pid={window_ref['pid']} window_id={window_ref['window_id']} "
                f"elements={count} screenshot_bytes={image_size}",
            )
        except Exception as exc:
            last_error = f"attempt {attempt}/{attempts}: {exc}"
        finally:
            client.close()
        if attempt < attempts:
            time.sleep(2)
    return False, last_error


def mobile_reference(
    command: list[str],
    device: str,
    package: str,
    *,
    timeout: int = 30,
    attempts: int = 3,
    env: dict[str, str] | None = None,
    client_factory: ClientFactory = McpClient,
) -> tuple[bool, str]:
    """Launch the frozen reference app, then require its full hierarchy and pixels."""
    if not device or not package:
        return False, "device and package are required"
    client = client_factory(command, env=env, timeout=timeout)
    try:
        tools = client.initialize()
        _require_tools(
            tools,
            {
                "mobile_list_available_devices",
                "mobile_launch_app",
                "mobile_ui_dump",
                "mobile_take_screenshot",
            },
        )
        devices = client.call_tool("mobile_list_available_devices", {})
        if device not in json.dumps(devices, ensure_ascii=False):
            raise McpError(f"target device {device!r} is absent from mobile-mcp")
        last_error = ""
        for attempt in range(1, attempts + 1):
            try:
                client.call_tool(
                    "mobile_launch_app", {"device": device, "packageName": package}
                )
                time.sleep(2)
                hierarchy = client.call_tool(
                    "mobile_ui_dump", {"device": device, "compressed": False}
                )
                xml = _text_content(hierarchy)
                node_count = len(re.findall(r"<node\b", xml))
                if "<hierarchy" not in xml or node_count < 3:
                    raise McpError(
                        "reference UI hierarchy is incomplete "
                        f"(nodes={node_count}, bytes={len(xml)})"
                    )
                screenshot = client.call_tool(
                    "mobile_take_screenshot", {"device": device}
                )
                image_size = _image_bytes(screenshot)
                if image_size < 128:
                    raise McpError(
                        f"reference screenshot is empty/invalid ({image_size} bytes)"
                    )
                return (
                    True,
                    f"device={device} package={package} nodes={node_count} "
                    f"screenshot_bytes={image_size}",
                )
            except Exception as exc:
                last_error = f"attempt {attempt}/{attempts}: {exc}"
                if attempt < attempts:
                    time.sleep(2)
        raise McpError(last_error)
    except Exception as exc:
        return False, str(exc)
    finally:
        client.close()


def playwright_reference(
    command: list[str],
    url: str,
    *,
    timeout: int = 30,
    require_image: bool = True,
    env: dict[str, str] | None = None,
    client_factory: ClientFactory = McpClient,
) -> tuple[bool, str]:
    """Navigate the real reference and require its accessibility snapshot and screenshot."""
    if not url:
        return False, "reference URL is required"
    client = client_factory(command, env=env, timeout=timeout)
    try:
        tools = client.initialize()
        _require_tools(
            tools,
            {"browser_navigate", "browser_snapshot", "browser_take_screenshot"},
        )
        client.call_tool("browser_navigate", {"url": url}, timeout=max(timeout, 60))
        snapshot = client.call_tool("browser_snapshot", {})
        text = _text_content(snapshot).strip()
        if len(text) < 20:
            raise McpError(
                f"reference accessibility snapshot is empty/invalid ({len(text)} chars)"
            )
        screenshot = client.call_tool("browser_take_screenshot", {})
        image_size = _image_bytes(screenshot)
        if require_image and image_size < 128:
            raise McpError(
                f"reference screenshot is empty/invalid ({image_size} bytes)"
            )
        return (
            True,
            f"url={url} snapshot_chars={len(text)} screenshot_bytes={image_size}",
        )
    except Exception as exc:
        return False, str(exc)
    finally:
        client.close()


def _command_from_args(args: argparse.Namespace) -> list[str]:
    if args.command_json:
        try:
            command = json.loads(args.command_json)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid --command-json: {exc}") from exc
        if (
            not isinstance(command, list)
            or not command
            or not all(isinstance(part, str) and part for part in command)
        ):
            raise ValueError("--command-json must be a non-empty JSON string array")
        return command
    if not args.driver:
        raise ValueError("--driver or --command-json is required")
    command = [args.driver, "mcp"]
    if args.socket:
        command += ["--socket", args.socket]
    command.append("--no-overlay")
    if args.claude_code_compat:
        command.append("--claude-code-computer-use-compat")
    return command


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--driver")
    parser.add_argument("--socket")
    parser.add_argument("--command-json")
    parser.add_argument(
        "--kind", choices=("tools", "desktop", "mobile", "playwright"), default="tools"
    )
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--pid", type=int, default=0)
    parser.add_argument("--include-descendants", action="store_true")
    parser.add_argument(
        "--allow-windowless",
        action="store_true",
        help="soft-pass a reference with no top-level window (macOS menu-bar apps)",
    )
    parser.add_argument("--claude-code-compat", action="store_true")
    parser.add_argument("--device", default="")
    parser.add_argument("--package", default="")
    parser.add_argument("--url", default="")
    args = parser.parse_args(argv)
    try:
        command = _command_from_args(args)
        if args.kind == "desktop":
            ok, detail = desktop_reference(
                command,
                args.pid,
                timeout=args.timeout,
                attempts=args.attempts,
                include_descendants=args.include_descendants,
                allow_windowless=args.allow_windowless,
            )
        elif args.kind == "mobile":
            ok, detail = mobile_reference(
                command,
                args.device,
                args.package,
                timeout=args.timeout,
                attempts=args.attempts,
            )
        elif args.kind == "playwright":
            ok, detail = playwright_reference(command, args.url, timeout=args.timeout)
        else:
            ok, detail = tools_list(command, timeout=args.timeout)
    except Exception as exc:
        ok, detail = False, str(exc)
    print(f"MCP_PREFLIGHT_{'OK' if ok else 'FAILED'} {detail}")
    return 0 if ok else 86


if __name__ == "__main__":
    raise SystemExit(main())
