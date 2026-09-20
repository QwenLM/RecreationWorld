"""Shared app launch/kill/preflight helpers for Windows pipeline stages.

Consolidates the duplicated launch logic used by recreation.py and
and uia_eval.py into a single module with consistent retry and fallback behavior.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time

KILL_WAIT = 2
APP_STARTUP_WAIT = 5


def _result_payload(stdout: str) -> dict:
    """Return a driver's structured result across CLI output generations."""
    value = json.loads(stdout or "{}")
    if not isinstance(value, dict):
        return {}
    for key in ("structuredContent", "structured_content"):
        nested = value.get(key)
        if isinstance(nested, dict):
            return nested
    return value


def _legacy_windows(payload: dict) -> list[dict]:
    """Normalise both legacy flat and current MCP window result shapes."""
    windows = payload.get("_legacy_windows") or payload.get("windows") or []
    normalised = []
    for window in windows:
        if not isinstance(window, dict):
            continue
        item = dict(window)
        bounds = item.get("bounds")
        if isinstance(bounds, dict):
            for key in ("x", "y", "width", "height"):
                item.setdefault(key, bounds.get(key, 0))
        normalised.append(item)
    return normalised


def codex_desktop_mcp_succeeded(trajectory_path: str) -> bool:
    """True only after Codex completed a real desktop-control tool call."""
    try:
        with open(trajectory_path, encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    item = json.loads(line).get("item", {})
                except (TypeError, json.JSONDecodeError):
                    continue
                if (
                    item.get("type") == "mcp_tool_call"
                    and item.get("server") == "desktop-control"
                    and item.get("status") == "completed"
                    and not item.get("error")
                ):
                    return True
    except OSError:
        pass
    return False


def kill_app(exe_path: str, pid: int | None = None) -> None:
    """Kill app process by PID tree (preferred) then by image name (fallback).

    Verifies the process is gone after each attempt; retries with /t (tree kill)
    if the first attempt leaves orphans.
    """
    if pid is not None:
        subprocess.run(
            ["taskkill", "/f", "/t", "/pid", str(pid)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(KILL_WAIT)

    exe_name = os.path.basename(exe_path)
    if not exe_name:
        return
    subprocess.run(
        ["taskkill", "/f", "/im", exe_name],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(KILL_WAIT)

    check = subprocess.run(
        ["tasklist", "/FI", f"IMAGENAME eq {exe_name}", "/NH"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if exe_name.lower() in check.stdout.lower():
        subprocess.run(
            ["taskkill", "/f", "/t", "/im", exe_name],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        time.sleep(KILL_WAIT)


def launch_via_script(launch_ps1: str, exe_path: str = "") -> tuple[int | None, str]:
    """Launch app via launch.ps1 and return (pid, exe_name).

    Handles single-instance / MSIX apps where the initial PID dies and the
    real process runs under a different PID (found by exe image name).

    Returns (None, "") if launch fails.
    """
    exe_name_hint = os.path.basename(exe_path) if exe_path else ""
    try:
        ps_exe = "pwsh" if shutil.which("pwsh") else "powershell"
        result = subprocess.run(
            [ps_exe, "-ExecutionPolicy", "Bypass", "-File", launch_ps1],
            capture_output=True,
            text=True,
            timeout=30,
            encoding="utf-8",
            errors="replace",
        )
        pid_str = (result.stdout.strip().splitlines() or [""])[0].strip()
        if not pid_str.isdigit():
            print(f"    launch.ps1 did not output a valid PID (got: {pid_str!r})")
            if result.stderr and result.stderr.strip():
                print(f"    launch.ps1 stderr: {result.stderr.strip()[:500]}")
            return None, ""
        pid = int(pid_str)
        time.sleep(APP_STARTUP_WAIT)
        check = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH", "/FO", "CSV"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if str(pid) not in check.stdout:
            if exe_name_hint:
                print(
                    f"    App (PID={pid}) not alive after {APP_STARTUP_WAIT}s, checking by name ({exe_name_hint})..."
                )
                name_check = subprocess.run(
                    [
                        "tasklist",
                        "/FI",
                        f"IMAGENAME eq {exe_name_hint}",
                        "/NH",
                        "/FO",
                        "CSV",
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                for line in name_check.stdout.strip().splitlines():
                    if exe_name_hint.lower() in line.lower():
                        parts = line.strip('"').split('","')
                        if len(parts) >= 2 and parts[1].strip('"').isdigit():
                            found_pid = int(parts[1].strip('"'))
                            print(
                                f"    Found running process by name: {exe_name_hint} (PID={found_pid})"
                            )
                            return found_pid, exe_name_hint
            print(f"    App (PID={pid}) not alive after {APP_STARTUP_WAIT}s")
            return None, ""
        exe_name = ""
        for line in check.stdout.strip().splitlines():
            if str(pid) in line:
                parts = line.strip('"').split('","')
                if parts:
                    exe_name = parts[0].strip('"')
                break
        return pid, exe_name
    except subprocess.TimeoutExpired:
        print("    launch.ps1 timed out")
        return None, ""
    except Exception as e:
        print(f"    launch.ps1 error: {e}")
        return None, ""


def launch_app(
    exe_path: str, launch_ps1: str | None = None, retries: int = 3
) -> tuple[int | None, str]:
    """Launch app with retries. Returns (pid, exe_name) or (None, "").

    Priority: launch.ps1 (if exists) → direct Popen(exe_path).
    Retries on failure with 3s delay between attempts.
    """
    for attempt in range(retries):
        if launch_ps1 and os.path.exists(launch_ps1):
            pid, exe_name = launch_via_script(launch_ps1, exe_path)
            if pid is not None:
                return pid, exe_name
            if attempt < retries - 1:
                print(f"    Retry launch via script (attempt {attempt + 1}/{retries})")
                time.sleep(3)
            continue

        try:
            proc = subprocess.Popen(
                [exe_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            time.sleep(APP_STARTUP_WAIT)
            if proc.poll() is not None:
                print(
                    f"    App exited immediately (code={proc.returncode}, attempt {attempt + 1}/{retries})"
                )
                if attempt < retries - 1:
                    time.sleep(3)
                continue
            exe_name = os.path.basename(exe_path)
            return proc.pid, exe_name
        except Exception as e:
            print(f"    Failed to launch app (attempt {attempt + 1}/{retries}): {e}")
            if attempt < retries - 1:
                time.sleep(3)
    return None, ""


def cua_preflight(
    pid: int, cua_driver_path: str, timeout: float = 30.0, max_attempts: int = 3
) -> bool:
    """Verify CUA MCP can see the app window (UIA tree accessible).

    Mirrors Linux's cua_mcp_preflight: calls list_windows + get_window_state
    via the CUA Driver CLI to confirm the app is visible and has UIA elements.

    Returns True if verification passes, False after max_attempts failures.
    """
    if not pid:
        print("    CUA preflight: no PID, skipping")
        return False

    interval = timeout / max_attempts
    last_error = ""

    for attempt in range(1, max_attempts + 1):
        try:
            list_result = subprocess.run(
                [
                    cua_driver_path,
                    "call",
                    "list_windows",
                    json.dumps({"pid": pid, "on_screen_only": True}),
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if list_result.returncode != 0:
                raise RuntimeError(
                    f"list_windows failed: {list_result.stderr.strip()[:200]}"
                )
            windows = _legacy_windows(_result_payload(list_result.stdout))
            # Never certify an unrelated desktop window after a PID-filtered miss.  The launch
            # helper already resolves wrapper/MSIX launches to the live application PID.
            visible = [
                w
                for w in windows
                if str(w.get("pid", "")) == str(pid)
                and w.get("width", 0) >= 50
                and w.get("height", 0) >= 50
            ]
            if not visible:
                last_error = f"no visible window owned by reference PID {pid}"
                raise RuntimeError(last_error)

            window = visible[0]
            win_w = window.get("width", 0)
            win_h = window.get("height", 0)

            if win_w < 200 or win_h < 50:
                last_error = f"window too small ({win_w}x{win_h})"
                raise RuntimeError(last_error)

            payload = json.dumps(
                {
                    "pid": window.get("pid", pid),
                    "window_id": window.get("window_id", 0),
                    "capture_mode": "ax",
                }
            )
            state_result = subprocess.run(
                [cua_driver_path, "call", "get_window_state", payload],
                capture_output=True,
                text=True,
                timeout=20,
            )
            if state_result.returncode != 0:
                last_error = (
                    f"get_window_state failed: {state_result.stderr.strip()[:200]}"
                )
                raise RuntimeError(last_error)

            state = _result_payload(state_result.stdout)
            element_count = state.get("element_count", 0)
            if element_count < 3:
                last_error = f"unusable UIA tree (element_count={element_count})"
                raise RuntimeError(last_error)

            title = window.get("title", "?")
            print(
                f"    CUA preflight: PASS "
                f"(window={title!r} pid={pid} size={win_w}x{win_h} elements={element_count})"
            )
            return True

        except Exception as exc:
            last_error = str(exc)
            if attempt < max_attempts:
                time.sleep(interval)

    print(f"    CUA preflight: FAILED after {max_attempts} attempts: {last_error}")
    return False
