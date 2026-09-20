#!/usr/bin/env python3
"""Best-effort screenshots after completed agent tool calls.

The agent CLIs already emit an append-only JSONL trajectory.  This module tails
that authoritative stream, recognizes completed Claude/Codex tool calls, and
runs a platform-provided screenshot command.  Capture is deliberately
diagnostic: command failures are recorded in ``manifest.jsonl`` and never alter
the agent process' exit status.

This file is stdlib-only because desktop adapters copy it next to
``agent_invocation.py`` on remote machines.
"""

from __future__ import annotations

import datetime as _datetime
import hashlib
import json
import os
import re
import subprocess
import threading
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9._-]+")
_CODEX_TOOL_TYPES = {
    "command_execution",
    "file_change",
    "function_call",
    "custom_tool_call",
    "mcp_tool_call",
    "web_search",
    "web_search_call",
    "computer_call",
}


@dataclass(frozen=True)
class ToolCompletion:
    event_key: str
    tool_name: str
    status: str
    agent_cli: str


class ToolEventParser:
    """Incrementally turn Claude/Codex JSONL records into tool completions."""

    def __init__(self) -> None:
        self._claude_pending: dict[str, str] = {}
        self._completed: set[str] = set()
        self._anonymous_index = 0

    @staticmethod
    def _walk_content(value: object) -> list[Mapping[str, object]]:
        found: list[Mapping[str, object]] = []
        if isinstance(value, Mapping):
            found.append(value)
            for key in ("content", "message"):
                nested = value.get(key)
                if nested is not value:
                    found.extend(ToolEventParser._walk_content(nested))
        elif isinstance(value, list):
            for item in value:
                found.extend(ToolEventParser._walk_content(item))
        return found

    @staticmethod
    def _content(record: Mapping[str, object]) -> list[Mapping[str, object]]:
        return ToolEventParser._walk_content(record.get("message")) + (
            ToolEventParser._walk_content(record.get("content"))
            if record.get("content") is not record.get("message")
            else []
        )

    def _key(self, prefix: str, value: object, record: Mapping[str, object]) -> str:
        if value not in (None, ""):
            return f"{prefix}:{value}"
        self._anonymous_index += 1
        digest = hashlib.sha256(
            json.dumps(record, sort_keys=True, ensure_ascii=False).encode("utf-8")
        ).hexdigest()[:16]
        return f"{prefix}:anonymous:{self._anonymous_index}:{digest}"

    def _emit(self, completion: ToolCompletion) -> list[ToolCompletion]:
        if completion.event_key in self._completed:
            return []
        self._completed.add(completion.event_key)
        return [completion]

    def feed(self, record: Mapping[str, object]) -> list[ToolCompletion]:
        """Return zero or more newly-completed tool calls from one JSON record."""
        kind = str(record.get("type") or "")

        # Claude's stream-json records assistant tool_use blocks first, followed
        # by a user tool_result block once the tool has actually returned.
        contents = self._content(record)
        if kind == "tool_use":
            contents = [record]
        for item in contents:
            if item.get("type") != "tool_use":
                continue
            tool_id = str(item.get("id") or item.get("tool_use_id") or "")
            if tool_id:
                self._claude_pending[tool_id] = str(item.get("name") or "tool")

        results: list[ToolCompletion] = []
        if kind == "tool_result":
            contents = [record]
        for item in contents:
            if item.get("type") != "tool_result":
                continue
            tool_id = str(item.get("tool_use_id") or item.get("id") or "")
            name = self._claude_pending.pop(tool_id, "tool")
            status = "failed" if item.get("is_error") else "completed"
            key = self._key("claude", tool_id, item)
            results.extend(self._emit(ToolCompletion(key, name, status, "claude")))

        # Codex emits a single item.completed event for a finished command/MCP
        # call.  Non-tool items (reasoning and agent messages) are excluded.
        if kind == "item.completed" and isinstance(record.get("item"), Mapping):
            item = record["item"]  # type: ignore[index]
            item_type = str(item.get("type") or "")
            if item_type in _CODEX_TOOL_TYPES or item_type.endswith("_tool_call"):
                identity = item.get("id") or item.get("call_id")
                tool = item.get("tool") or item.get("name") or item_type
                server = item.get("server")
                name = f"{server}.{tool}" if server else str(tool)
                status = str(item.get("status") or "completed")
                if item.get("error") and status == "completed":
                    status = "failed"
                key = self._key("codex", identity, item)
                results.extend(self._emit(ToolCompletion(key, name, status, "codex")))
        return results


def _safe_tool_name(value: str) -> str:
    result = _SAFE_NAME_RE.sub("_", str(value or "tool")).strip("._-")
    return (result or "tool")[:80]


def _utc_now() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat()


CaptureCallable = Callable[[Path], tuple[bool, str]]


class ToolUseCaptureMonitor:
    """Tail one trajectory and capture after every completed tool use."""

    def __init__(
        self,
        trajectory_path: str | os.PathLike[str],
        output_dir: str | os.PathLike[str],
        *,
        command: Sequence[str] = (),
        command_stdout: bool = False,
        timeout_sec: float = 10,
        poll_interval: float = 0.1,
        capture: CaptureCallable | None = None,
        start_offset: int = 0,
    ) -> None:
        self.trajectory_path = Path(trajectory_path)
        self.output_dir = Path(output_dir)
        self.command = tuple(str(part) for part in command)
        self.command_stdout = bool(command_stdout)
        self.timeout_sec = float(timeout_sec)
        self.poll_interval = float(poll_interval)
        self.capture = capture
        self.start_offset = max(0, int(start_offset))
        self._stop = threading.Event()
        self._stop_deadline: float | None = None
        self._thread: threading.Thread | None = None
        self._parser = ToolEventParser()
        self._sequence = self._existing_sequence()

    @property
    def manifest_path(self) -> Path:
        return self.output_dir / "manifest.jsonl"

    def _existing_sequence(self) -> int:
        """Continue an append-only screenshot series without overwriting files."""
        highest = 0
        try:
            with self.manifest_path.open(encoding="utf-8") as stream:
                for line in stream:
                    try:
                        value = json.loads(line)
                        highest = max(highest, int(value.get("sequence") or 0))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        continue
        except OSError:
            pass
        return highest

    def start(self) -> ToolUseCaptureMonitor:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self._thread = threading.Thread(
            target=self._run, name="rb-tool-use-capture", daemon=True
        )
        self._thread.start()
        return self

    def stop(self, timeout: float | None = None) -> None:
        # Optional diagnostics must not hold the agent open while draining an
        # arbitrarily large backlog of slow screenshot commands.
        budget = self.timeout_sec + 1 if timeout is None else max(0, timeout)
        self._stop_deadline = time.monotonic() + budget
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=budget)

    def _append_manifest(self, value: Mapping[str, object]) -> None:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        with self.manifest_path.open("a", encoding="utf-8", newline="") as stream:
            stream.write(json.dumps(dict(value), ensure_ascii=False, sort_keys=True))
            stream.write("\n")

    def _command_capture(self, temporary: Path) -> tuple[bool, str]:
        if not self.command:
            return False, "screenshot command is not configured"
        timeout = self.timeout_sec
        if self._stop_deadline is not None:
            timeout = min(timeout, self._stop_deadline - time.monotonic())
            if timeout <= 0:
                return False, "capture shutdown deadline exceeded"
        argv = [part.replace("{output}", str(temporary)) for part in self.command]
        if not self.command_stdout and not any("{output}" in p for p in self.command):
            argv.append(str(temporary))
        try:
            if self.command_stdout:
                with temporary.open("wb") as image:
                    proc = subprocess.run(
                        argv,
                        stdout=image,
                        stderr=subprocess.PIPE,
                        timeout=timeout,
                        check=False,
                    )
            else:
                proc = subprocess.run(
                    argv,
                    capture_output=True,
                    timeout=timeout,
                    check=False,
                )
        except (OSError, subprocess.SubprocessError) as exc:
            return False, f"{type(exc).__name__}: {exc}"
        detail = (proc.stderr or proc.stdout or b"").decode("utf-8", "replace")
        if proc.returncode:
            return False, f"screenshot command exit {proc.returncode}: {detail[:300]}"
        if not temporary.is_file() or temporary.stat().st_size < len(PNG_SIGNATURE):
            return False, f"screenshot command produced no PNG: {detail[:300]}"
        try:
            with temporary.open("rb") as image:
                signature = image.read(len(PNG_SIGNATURE))
        except OSError as exc:
            return False, f"cannot read screenshot: {exc}"
        if signature != PNG_SIGNATURE:
            return False, "screenshot output is not a PNG"
        return True, detail.strip()[:300]

    def _capture_one(self, completion: ToolCompletion) -> None:
        self._sequence += 1
        name = f"{self._sequence:06d}_{_safe_tool_name(completion.tool_name)}.png"
        destination = self.output_dir / name
        temporary = destination.with_suffix(".tmp.png")
        ok, detail = False, ""
        try:
            if self.capture is not None:
                ok, detail = self.capture(temporary)
            else:
                ok, detail = self._command_capture(temporary)
            if ok:
                if not temporary.is_file() or temporary.stat().st_size == 0:
                    ok, detail = False, "capture callback produced no image"
                else:
                    with temporary.open("rb") as image:
                        signature = image.read(len(PNG_SIGNATURE))
                    if signature != PNG_SIGNATURE:
                        ok, detail = False, "capture callback output is not a PNG"
                    else:
                        os.replace(temporary, destination)
        except Exception as exc:  # noqa: BLE001 - capture must always fail open
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        finally:
            if temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass
        self._append_manifest(
            {
                **asdict(completion),
                "sequence": self._sequence,
                "captured_at": _utc_now(),
                "screenshot": name if ok else None,
                "ok": ok,
                "error": None if ok else (detail or "unknown screenshot failure"),
            }
        )

    def _feed_bytes(self, pending: bytes, data: bytes, *, final: bool) -> bytes:
        pending += data
        lines = pending.split(b"\n")
        if not final:
            pending = lines.pop()
        else:
            pending = b""
        for raw in lines:
            if not raw.strip():
                continue
            try:
                record = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, ValueError):
                continue
            if not isinstance(record, Mapping):
                continue
            for completion in self._parser.feed(record):
                if (
                    self._stop_deadline is not None
                    and time.monotonic() >= self._stop_deadline
                ):
                    return b""
                self._capture_one(completion)
        return pending

    def _run(self) -> None:
        position = self.start_offset
        pending = b""
        while True:
            if (
                self._stop_deadline is not None
                and time.monotonic() >= self._stop_deadline
            ):
                return
            data = b""
            try:
                size = self.trajectory_path.stat().st_size
                if size < position:
                    position, pending = 0, b""
                if size > position:
                    with self.trajectory_path.open("rb") as stream:
                        stream.seek(position)
                        data = stream.read()
                    position += len(data)
            except OSError:
                pass
            if data:
                pending = self._feed_bytes(pending, data, final=False)
                continue
            if self._stop.is_set():
                self._feed_bytes(pending, b"", final=True)
                return
            time.sleep(self.poll_interval)
