#!/usr/bin/env python3
"""One invocation contract for Claude Code and Codex on every RB platform.

The operating systems still own their process boundary (``runuser``/``sudo``, a
Windows desktop session, VM transport and Web network namespaces).  Everything
inside that boundary is defined here:

* prompts are exact UTF-8 files and are supplied on stdin, never in argv;
* one canonical argv is used for each supported CLI;
* the agent, model-request and MCP-tool clocks have one set of defaults;
* stdout/stderr use the canonical trajectory paths; and
* native process status and the JSONL terminal protocol produce one verdict.

This file is deliberately stdlib-only.  Desktop launchers may copy the file into
a VM and execute it directly when the full RecreationBench tree is unavailable.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import signal
import subprocess
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Mapping, Sequence

DEFAULT_AGENT_TIMEOUT_SEC = 72_000
DEFAULT_REQUEST_TIMEOUT_MS = 1_800_000
DEFAULT_TOOL_TIMEOUT_MS = 180_000
TIMEOUT_EXIT_CODE = 124
TERMINATED_EXIT_CODE = 143
INFRA_EXIT_CODE = 2

TERMINAL_COMPLETED = "completed"
TERMINAL_BUDGET_EXHAUSTED = "budget_exhausted"
TERMINAL_TERMINATED = "terminated"
TERMINAL_INFRA_ERROR = "infra_error"
CLAUDE_CONTEXT_SUFFIX = "[1m]"
DEFAULT_CLAUDE_MODEL_ALIAS = "claude-opus-4-8"

_API_ERROR_RE = re.compile(
    r"api\s*error|timed?\s*out|timeout|deadline\s+exceeded|rate.?limit|"
    r"\b429\b|\b529\b|\b5\d\d\b|overloaded|too\s+many\s+requests|"
    r"connection\s+(?:error|reset|refused|aborted|closed)|econnreset|"
    r"network\s+error|socket\s+hang|quota\s+exceeded|service\s+unavailable|"
    r"\binvalid_encrypted_content\b|request\s+body\s+too\s+large|"
    r"content-length.{0,80}exceeds\s+maximum",
    re.IGNORECASE,
)


def normalize_agent_cli(value: str) -> str:
    """Collapse compatibility spellings to the two supported runtimes."""
    name = str(value or "claude").strip().lower()
    return "codex" if name.startswith("codex") else "claude"


def normalize_model(agent_cli: str, model: str, *, context_1m: bool = False) -> str:
    """Return the client-facing model name for one CLI."""
    value = str(model or "")
    if normalize_agent_cli(agent_cli) == "claude":
        if context_1m and not value.endswith(CLAUDE_CONTEXT_SUFFIX):
            value += CLAUDE_CONTEXT_SUFFIX
        return value
    if value.startswith("openai."):
        value = value[len("openai.") :]
    if value.endswith(CLAUDE_CONTEXT_SUFFIX):
        value = value[: -len(CLAUDE_CONTEXT_SUFFIX)]
    return value


def claude_model_alias(claude_model: str = "") -> str:
    """Return the undecorated model alias exposed to Claude Code.

    Upstream routing uses ``PipelineConfig.model`` independently.  Keeping a
    Claude-native name at the client boundary lets Claude Code apply the right
    context-window policy even when the proxy ultimately serves Kimi, Qwen, or
    another non-Claude model.
    """
    value = str(claude_model or DEFAULT_CLAUDE_MODEL_ALIAS).strip()
    if value.endswith(CLAUDE_CONTEXT_SUFFIX):
        value = value[: -len(CLAUDE_CONTEXT_SUFFIX)]
    return value


def claude_client_model(claude_model: str = "", *, context_1m: bool = False) -> str:
    """Return the final model argument for Claude Code."""
    return normalize_model(
        "claude", claude_model_alias(claude_model), context_1m=context_1m
    )


@dataclass(frozen=True)
class InvocationSpec:
    agent_cli: str
    model: str
    prompt_file: str
    workspace: str
    trajectory_path: str
    stderr_path: str
    timeout_sec: int = DEFAULT_AGENT_TIMEOUT_SEC
    request_timeout_ms: int = DEFAULT_REQUEST_TIMEOUT_MS
    tool_timeout_ms: int = DEFAULT_TOOL_TIMEOUT_MS
    mcp_config: str = ""
    reasoning_effort: str = ""
    session_mode: str = "initial"  # initial | continue | resume
    session_id: str = ""
    run_name: str = ""
    max_turns: int | None = None
    executable: str = ""
    extra_args: tuple[str, ...] = field(default_factory=tuple)
    capture_tool_use_screenshots: bool = False
    screenshot_dir: str = ""
    screenshot_command: tuple[str, ...] = field(default_factory=tuple)
    screenshot_stdout: bool = False
    screenshot_timeout_sec: float = 10
    isolate_environment: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "agent_cli", normalize_agent_cli(self.agent_cli))
        object.__setattr__(self, "timeout_sec", int(self.timeout_sec))
        object.__setattr__(self, "request_timeout_ms", int(self.request_timeout_ms))
        object.__setattr__(self, "tool_timeout_ms", int(self.tool_timeout_ms))
        object.__setattr__(self, "extra_args", tuple(self.extra_args))
        object.__setattr__(self, "screenshot_command", tuple(self.screenshot_command))
        object.__setattr__(
            self,
            "capture_tool_use_screenshots",
            bool(self.capture_tool_use_screenshots),
        )
        object.__setattr__(self, "screenshot_stdout", bool(self.screenshot_stdout))
        object.__setattr__(self, "isolate_environment", bool(self.isolate_environment))
        object.__setattr__(
            self, "screenshot_timeout_sec", float(self.screenshot_timeout_sec)
        )
        if self.session_mode not in ("initial", "continue", "resume"):
            raise ValueError("session_mode must be initial, continue, or resume")
        if self.session_mode == "resume" and not self.session_id:
            raise ValueError("session_id is required for session_mode=resume")
        if self.timeout_sec < 0:
            raise ValueError("timeout_sec must be non-negative")
        for name in ("request_timeout_ms", "tool_timeout_ms"):
            if getattr(self, name) <= 0:
                raise ValueError(f"{name} must be positive")
        if self.screenshot_timeout_sec <= 0:
            raise ValueError("screenshot_timeout_sec must be positive")


def build_argv(spec: InvocationSpec) -> list[str]:
    """Return canonical CLI argv, deliberately excluding the prompt text.

    Both clients consume ``spec.prompt_file`` through stdin.  This avoids command
    length/quoting differences and keeps task text out of process listings.
    """
    if spec.agent_cli == "codex":
        if spec.session_mode == "initial":
            argv = [
                spec.executable or "codex",
                "exec",
                "--dangerously-bypass-approvals-and-sandbox",
                "--skip-git-repo-check",
                "--json",
            ]
            if spec.model:
                argv += ["--model", spec.model]
            if spec.reasoning_effort:
                argv += ["-c", f"model_reasoning_effort={spec.reasoning_effort}"]
        else:
            argv = [spec.executable or "codex", "exec", "resume"]
            argv += [spec.session_id] if spec.session_mode == "resume" else ["--last"]
            argv += ["--json"]
        return argv + list(spec.extra_args)

    argv = [spec.executable or "claude"]
    if spec.session_mode == "continue":
        argv.append("--continue")
    elif spec.session_mode == "resume":
        argv += ["--resume", spec.session_id]
    argv.append("-p")
    if spec.session_mode == "initial" and spec.session_id:
        argv += ["--session-id", spec.session_id]
    if spec.model:
        argv += ["--model", spec.model]
    argv += ["--output-format", "stream-json", "--verbose", "--disable-slash-commands"]
    if spec.reasoning_effort:
        argv += ["--effort", spec.reasoning_effort]
    if spec.mcp_config:
        argv += ["--mcp-config", spec.mcp_config]
    if spec.run_name and spec.session_mode == "initial":
        argv += ["--name", spec.run_name]
    if spec.max_turns is not None:
        argv += ["--max-turns", str(int(spec.max_turns))]
    return argv + list(spec.extra_args)


_AGENT_PRIVATE_EXACT = {
    "APP_DIR",
    "DATA_DIR",
    "INSTANCE_ID",
    "MODEL_API_KEY",
    "OUTPUT_DIR",
    "REC_DIR",
    "REFERENCE_DIR",
    "TASK_ID",
    "TESTS_DIR",
    "VLM_JUDGE_API_KEY",
}
_AGENT_PRIVATE_PREFIXES = ("RB_ARTIFACT_", "RB_UNIFIED_", "VLM_JUDGE_")
_AGENT_PRIVATE_SUFFIXES = (
    "_ACCESS_KEY_ID",
    "_ACCESS_KEY_SECRET",
    "_ACCESS_TOKEN",
    "_API_KEY",
    "_AUTH_TOKEN",
    "_BUCKET",
    "_CREDENTIAL",
    "_CREDENTIALS",
    "_ENDPOINT",
    "_HEADERS",
    "_PASSWORD",
    "_PRIVATE_KEY",
    "_REFRESH_TOKEN",
    "_SECRET",
)


def isolated_agent_environment(
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Remove benchmark inputs and upstream credentials from an agent child.

    Matching uses generic secret and benchmark-contract names, so adding a
    storage or deployment integration does not require teaching core about it.
    The supported CLIs still receive non-secret local client credentials.
    """
    env = dict(os.environ if base is None else base)
    for name in tuple(env):
        upper = name.upper()
        if (
            upper in _AGENT_PRIVATE_EXACT
            or upper.startswith(_AGENT_PRIVATE_PREFIXES)
            or upper.endswith("_MODELS")
            or upper.endswith(_AGENT_PRIVATE_SUFFIXES)
        ):
            env.pop(name, None)
    env.update(
        {
            "ANTHROPIC_API_KEY": "proxy",
            "ANTHROPIC_AUTH_TOKEN": "proxy",
            "OPENAI_API_KEY": "proxy",
        }
    )
    return env


def runtime_env(
    spec: InvocationSpec, base: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Environment shared by direct and generated launchers."""
    env = (
        isolated_agent_environment(base)
        if spec.isolate_environment
        else dict(os.environ if base is None else base)
    )
    env.update(
        {
            # Request retries belong to the sidecar, never to the agent process.
            "CLAUDE_CODE_MAX_RETRIES": "0",
            "MAX_STRUCTURED_OUTPUT_RETRIES": "0",
            # Claude Code has independent first-byte and stream-idle watchdogs.
            # Keep every model-I/O clock on the canonical request timeout.  In
            # particular, custom Anthropic routes otherwise retain a shorter
            # transport idle timer and can return the synthetic assistant text
            # "Request timed out" even though the 20h agent budget is intact.
            "CLAUDE_SLOW_FIRST_BYTE_MS": str(spec.request_timeout_ms),
            "CLAUDE_BYTE_STREAM_IDLE_TIMEOUT_MS": str(spec.request_timeout_ms),
            "CLAUDE_STREAM_IDLE_TIMEOUT_MS": str(spec.request_timeout_ms),
            "API_FORCE_IDLE_TIMEOUT": "false",
            "CLAUDE_ENABLE_STREAM_WATCHDOG": "true",
            "API_TIMEOUT_MS": str(spec.request_timeout_ms),
            "MCP_TOOL_TIMEOUT": str(spec.tool_timeout_ms),
        }
    )
    return env


def write_prompt(path: str | os.PathLike[str], prompt: str) -> Path:
    """Atomically write an exact, BOM-free UTF-8 prompt file."""
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".tmp")
    with tmp.open("w", encoding="utf-8", newline="") as fh:
        fh.write(prompt)
    os.replace(tmp, dest)
    return dest


def persist_stream(
    path: str | os.PathLike[str], data: bytes | str, *, append: bool = True
) -> int:
    """Persist one CLI stream without inserting non-JSON separators."""
    payload = data.encode("utf-8") if isinstance(data, str) else bytes(data)
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    needs_separator = False
    if append and dest.is_file() and dest.stat().st_size and payload:
        with dest.open("rb") as existing:
            existing.seek(-1, os.SEEK_END)
            needs_separator = existing.read(1) != b"\n"
    mode = "ab" if append else "wb"
    with dest.open(mode) as fh:
        if needs_separator and not payload.startswith(b"\n"):
            fh.write(b"\n")
        fh.write(payload)
        if payload and not payload.endswith(b"\n"):
            fh.write(b"\n")
    return len(payload)


def _terminate(proc: subprocess.Popen) -> None:
    """Best-effort process-tree termination for the portable runner."""
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=30,
            )
        else:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=10)
                return
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        try:
            proc.kill()
        except OSError:
            pass


def normalize_native_rc(returncode: int) -> int:
    """Convert Python's negative signal status to the shell's 128+signal form."""
    rc = int(returncode)
    return 128 + abs(rc) if rc < 0 else rc


def run(spec: InvocationSpec, *, env: Mapping[str, str] | None = None) -> int:
    """Run one invocation with the canonical prompt/stream/timeout contract."""
    prompt = Path(spec.prompt_file)
    if not prompt.is_file():
        raise FileNotFoundError(f"prompt file does not exist: {prompt}")
    trajectory = Path(spec.trajectory_path)
    stderr = Path(spec.stderr_path)
    trajectory.parent.mkdir(parents=True, exist_ok=True)
    stderr.parent.mkdir(parents=True, exist_ok=True)
    append = spec.session_mode != "initial"
    capture_start_offset = (
        trajectory.stat().st_size if append and trajectory.is_file() else 0
    )
    creationflags = (
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
    )
    try:
        with (
            prompt.open("rb") as stdin,
            trajectory.open("ab" if append else "wb") as stdout,
            stderr.open("ab" if append else "wb") as err,
        ):
            proc = subprocess.Popen(
                build_argv(spec),
                cwd=spec.workspace,
                env=runtime_env(spec, env),
                stdin=stdin,
                stdout=stdout,
                stderr=err,
                start_new_session=(os.name != "nt"),
                creationflags=creationflags,
            )
            capture_monitor = None
            if spec.capture_tool_use_screenshots:
                try:
                    try:
                        from core.tool_use_capture import ToolUseCaptureMonitor
                    except ImportError:  # copied next to this runner on a remote target
                        from tool_use_capture import ToolUseCaptureMonitor  # type: ignore

                    capture_monitor = ToolUseCaptureMonitor(
                        trajectory,
                        spec.screenshot_dir
                        or str(trajectory.parent / "tool_use_screenshots"),
                        command=spec.screenshot_command,
                        command_stdout=spec.screenshot_stdout,
                        timeout_sec=spec.screenshot_timeout_sec,
                        start_offset=capture_start_offset,
                    ).start()
                except Exception as exc:  # noqa: BLE001 - screenshot setup is fail-open
                    print(
                        f"tool-use screenshot monitor unavailable: {exc}",
                        file=sys.stderr,
                    )
            interrupted = [0]
            previous_handlers: dict[int, object] = {}

            def forward(signum, _frame):
                interrupted[0] = int(signum)
                _terminate(proc)

            for signum in (signal.SIGINT, signal.SIGTERM):
                try:
                    previous_handlers[signum] = signal.signal(signum, forward)
                except (OSError, ValueError):
                    pass
            try:
                timeout = None if spec.timeout_sec == 0 else spec.timeout_sec
                rc = normalize_native_rc(proc.wait(timeout=timeout))
                return 128 + interrupted[0] if interrupted[0] else rc
            except subprocess.TimeoutExpired:
                _terminate(proc)
                proc.wait()
                return TIMEOUT_EXIT_CODE
            finally:
                if capture_monitor is not None:
                    capture_monitor.stop()
                for signum, handler in previous_handlers.items():
                    signal.signal(signum, handler)
    except FileNotFoundError:
        return INFRA_EXIT_CODE


@dataclass(frozen=True)
class TerminalResult:
    status: str
    exit_code: int
    protocol_status: str
    native_rc: int
    reason: str


def terminal_result(
    trajectory_path: str | os.PathLike[str],
    native_rc: int,
    *,
    timed_out: bool = False,
) -> TerminalResult:
    """Combine process and protocol evidence into the canonical agent verdict.

    A wall-clock budget is a model boundary and remains 124 even if the stream was
    cut before a terminal record.  External SIGINT/SIGTERM always wins.  Otherwise
    the protocol terminal record is authoritative, including over a misleading
    native zero.
    """
    rc = normalize_native_rc(native_rc)
    try:
        from core import trajectory as trajectory_mod
    except ImportError:  # direct execution from scripts/core on a remote VM
        import trajectory as trajectory_mod  # type: ignore

    protocol = trajectory_mod.agent_terminal_status(str(trajectory_path))
    if rc in (130, 143):
        return TerminalResult(
            TERMINAL_TERMINATED,
            TERMINATED_EXIT_CODE,
            protocol,
            rc,
            "external_termination",
        )
    if timed_out or rc == TIMEOUT_EXIT_CODE:
        return TerminalResult(
            TERMINAL_BUDGET_EXHAUSTED,
            TIMEOUT_EXIT_CODE,
            protocol,
            rc,
            "agent_deadline_exhausted",
        )
    stop_reason = trajectory_mod.terminal_stop_reason(str(trajectory_path))
    if stop_reason == "refusal":
        return TerminalResult(
            TERMINAL_COMPLETED,
            0,
            protocol,
            rc,
            "agent_policy_refusal",
        )
    if protocol == "completed":
        return TerminalResult(TERMINAL_COMPLETED, 0, protocol, rc, "protocol_completed")
    reason = "protocol_error" if protocol == "error" else "missing_terminal_record"
    return TerminalResult(TERMINAL_INFRA_ERROR, INFRA_EXIT_CODE, protocol, rc, reason)


def terminal_api_failure(trajectory_path: str | os.PathLike[str]) -> str | None:
    """Return the final protocol error when it is an upstream/API failure."""
    try:
        from core import trajectory as trajectory_mod
    except ImportError:
        import trajectory as trajectory_mod  # type: ignore
    message = trajectory_mod.terminal_error_message(str(trajectory_path))
    return message[:200] if message and _API_ERROR_RE.search(message) else None


def _spec_from_mapping(value: Mapping[str, object]) -> InvocationSpec:
    data = dict(value)
    data["extra_args"] = tuple(data.get("extra_args") or ())
    data["screenshot_command"] = tuple(data.get("screenshot_command") or ())
    return InvocationSpec(**data)  # type: ignore[arg-type]


def load_spec(path: str | os.PathLike[str]) -> InvocationSpec:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("invocation spec must be a JSON object")
    return _spec_from_mapping(value)


def write_spec(path: str | os.PathLike[str], spec: InvocationSpec) -> Path:
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(
        json.dumps(asdict(spec), ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return dest


def spec_json(spec: InvocationSpec, *, indent: int | None = 2) -> str:
    """Serialize a spec for VM transport without exposing dataclass internals."""
    return json.dumps(asdict(spec), ensure_ascii=False, indent=indent)


def _main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="core.agent_invocation")
    sub = parser.add_subparsers(dest="command", required=True)

    run_parser = sub.add_parser("run", help="execute a JSON InvocationSpec")
    run_parser.add_argument("--spec", required=True)

    argv_parser = sub.add_parser(
        "argv", help="print a JSON argv from an InvocationSpec"
    )
    argv_parser.add_argument("--spec", required=True)

    create = sub.add_parser("create", help="write an InvocationSpec JSON file")
    create.add_argument("--output", required=True)
    create.add_argument("--agent-cli", required=True)
    create.add_argument("--model", required=True)
    create.add_argument("--context-1m", action="store_true")
    create.add_argument("--prompt-file", required=True)
    create.add_argument("--workspace", required=True)
    create.add_argument("--trajectory", required=True)
    create.add_argument("--stderr", required=True)
    create.add_argument("--timeout", type=int, default=DEFAULT_AGENT_TIMEOUT_SEC)
    create.add_argument(
        "--request-timeout-ms", type=int, default=DEFAULT_REQUEST_TIMEOUT_MS
    )
    create.add_argument("--tool-timeout-ms", type=int, default=DEFAULT_TOOL_TIMEOUT_MS)
    create.add_argument("--mcp-config", default="")
    create.add_argument("--reasoning-effort", default="")
    create.add_argument(
        "--session-mode", choices=("initial", "continue", "resume"), default="initial"
    )
    create.add_argument("--session-id", default="")
    create.add_argument("--run-name", default="")
    create.add_argument("--max-turns", type=int)
    create.add_argument("--executable", default="")
    create.add_argument("--extra-arg", action="append", default=[])
    create.add_argument("--capture-tool-use-screenshots", action="store_true")
    create.add_argument("--screenshot-dir", default="")
    create.add_argument("--screenshot-command", action="append", default=[])
    create.add_argument("--screenshot-stdout", action="store_true")
    create.add_argument("--screenshot-timeout", type=float, default=10)
    create.add_argument("--isolate-environment", action="store_true")

    term = sub.add_parser("terminal", help="classify native + JSONL terminal state")
    term.add_argument("--trajectory", required=True)
    term.add_argument("--native-rc", required=True, type=int)
    term.add_argument("--timed-out", action="store_true")
    term.add_argument(
        "--field",
        choices=("json", "status", "exit-code", "protocol", "reason"),
        default="json",
    )
    args = parser.parse_args(argv)

    if args.command == "run":
        return run(load_spec(args.spec))
    if args.command == "argv":
        print(json.dumps(build_argv(load_spec(args.spec)), ensure_ascii=False))
        return 0
    if args.command == "create":
        cli = normalize_agent_cli(args.agent_cli)
        spec = InvocationSpec(
            agent_cli=cli,
            model=normalize_model(cli, args.model, context_1m=args.context_1m),
            prompt_file=args.prompt_file,
            workspace=args.workspace,
            trajectory_path=args.trajectory,
            stderr_path=args.stderr,
            timeout_sec=args.timeout,
            request_timeout_ms=args.request_timeout_ms,
            tool_timeout_ms=args.tool_timeout_ms,
            mcp_config=args.mcp_config,
            reasoning_effort=args.reasoning_effort,
            session_mode=args.session_mode,
            session_id=args.session_id,
            run_name=args.run_name,
            max_turns=args.max_turns,
            executable=args.executable,
            extra_args=tuple(args.extra_arg),
            capture_tool_use_screenshots=args.capture_tool_use_screenshots,
            screenshot_dir=args.screenshot_dir,
            screenshot_command=tuple(args.screenshot_command),
            screenshot_stdout=args.screenshot_stdout,
            screenshot_timeout_sec=args.screenshot_timeout,
            isolate_environment=args.isolate_environment,
        )
        write_spec(args.output, spec)
        return 0

    result = terminal_result(args.trajectory, args.native_rc, timed_out=args.timed_out)
    if args.field == "json":
        print(json.dumps(asdict(result), sort_keys=True))
    elif args.field == "exit-code":
        print(result.exit_code)
    elif args.field == "protocol":
        print(result.protocol_status)
    else:
        print(getattr(result, args.field.replace("-", "_")))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
