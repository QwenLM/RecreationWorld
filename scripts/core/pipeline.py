#!/usr/bin/env python3
"""Unified Recreation-Bench release pipeline — one CLI for all five platforms.

    python scripts/core/pipeline.py --platform linux  --task-id notepadqq-notepadqq ...
    python scripts/core/pipeline.py --platform android --task-id dozingcat-vector-pinball ...

Dispatches directly to ``platforms/<platform>/pipeline.py``, runs its standard
``run(task, config) -> normalized state`` interface, then writes the single
``metrics.json`` contract via :mod:`core.metrics_contract`.  There is deliberately no
adapter registry between this entrypoint and a platform pipeline.

The genuinely platform-specific knobs (vm_port, image, backend, timeouts) are
passed through ``-x key=value`` into ``PipelineConfig.extra``. Cross-platform
runtime controls are first-class fields so every adapter sees the same values.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import signal
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # scripts/

from core import exit_contract, metrics_contract, retry_policy  # noqa: E402

# When this file is executed by path, platform modules import ``core.pipeline`` for
# the shared config type and lifecycle helpers.  Reuse this module rather than
# loading a second copy under another name.
if __name__ == "__main__":  # pragma: no cover - exercised by release entrypoints
    sys.modules.setdefault("core.pipeline", sys.modules[__name__])


PLATFORMS = ("linux", "windows", "macos", "web", "android")
RELEASE_STAGES = ("recreation", "eval")
STAGE_VALUES = ("setup", *RELEASE_STAGES, "recreation_eval")
CUA_PREFLIGHT_MODES = ("strict", "warn")
EVAL_TARGETS = ("recreation", "reference")
API_MODES = ("native", "openai")
TARGET_KINDS = ("local", "ssh", "adb")
LEGACY_TARGET_EXTRA_KEYS = frozenset(
    {
        "device_id",
        "instance_id",
        "sandbox_ip",
        "sandbox_password",
        "sandbox_username",
        "ssh_host",
        "ssh_key_path",
        "ssh_password",
        "ssh_port",
        "ssh_user",
    }
)
LEGACY_PROVIDER_EXTRA_KEYS = frozenset({"execution_mode", "sandbox_info_path"})
LEGACY_RUNTIME_EXTRA_KEYS = frozenset(
    {"context_1m", "auto_compact_window", "max_tokens_limit", "thinking_effort"}
)
LEGACY_MODEL_EXTRA_KEYS = frozenset(
    {
        "route",
        "base_url",
        "base_url_env",
        "api_key_env",
        "use_native_anthropic",
        "custom_llm_provider",
    }
)


class PipelineTerminated(RuntimeError):
    """Raised in the controller so cleanup runs before emitting rc=143."""

    def __init__(self, signum: int):
        super().__init__(f"shared pipeline received signal {signum}")
        self.signum = signum


def normalize_cua_preflight_mode(value: object) -> str:
    """Return the one cross-platform recreation MCP preflight policy."""
    mode = str(value or "strict").strip().lower()
    if mode not in CUA_PREFLIGHT_MODES:
        raise ValueError(
            f"unknown CUA preflight mode {value!r}; expected one of "
            f"{CUA_PREFLIGHT_MODES}"
        )
    return mode


def normalize_eval_target(value: object) -> str:
    """Return the candidate selected by an eval stage."""
    target = str(value or "recreation").strip().lower()
    if target not in EVAL_TARGETS:
        raise ValueError(
            f"unknown eval target {value!r}; expected one of {EVAL_TARGETS}"
        )
    return target


def normalize_api_mode(value: object) -> str:
    """Return the one upstream wire-protocol choice used by every platform."""
    mode = str(value or "native").strip().lower()
    if mode not in API_MODES:
        raise ValueError(f"unknown api_mode {value!r}; expected one of {API_MODES}")
    return mode


def normalize_bool(value: object, *, name: str) -> bool:
    """Normalize a public boolean without Python's surprising ``bool('false')``."""
    if isinstance(value, bool):
        return value
    normalized = str(value or "false").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    raise ValueError(f"{name} must be true or false, got {value!r}")


def stages_for(stage: str) -> tuple[str, ...]:
    """Expand the one public stage token into its execution sequence."""
    if stage == "setup":
        return ("setup",)
    if stage == "recreation_eval":
        return RELEASE_STAGES
    if stage in RELEASE_STAGES:
        return (stage,)
    raise ValueError(f"unknown stage {stage!r}; expected one of {STAGE_VALUES}")


@dataclass(frozen=True)
class TargetSpec:
    """Prepared execution target supplied by the runtime provider.

    RB consumes this descriptor; it does not create the underlying VM, browser,
    emulator, sidecar, or pod. Local is the inert default; remote platform
    adapters reject it unless their caller supplies the required SSH/ADB target.
    """

    kind: str = "local"
    host: str = ""
    user: str = ""
    password: str = ""
    port: int = 0
    key_path: str = ""
    device: str = ""

    def __post_init__(self) -> None:
        if self.kind not in TARGET_KINDS:
            raise ValueError(f"unknown target kind {self.kind!r}")
        ssh_fields = bool(
            self.host or self.user or self.password or self.port or self.key_path
        )
        if self.kind == "local" and (ssh_fields or self.device):
            raise ValueError("local target accepts no SSH or ADB fields")
        if self.kind == "ssh":
            if not self.host:
                raise ValueError("ssh target requires host")
            if self.device:
                raise ValueError("ssh target accepts no ADB device")
        if self.kind == "adb":
            if not self.device:
                raise ValueError("adb target requires device")
            if ssh_fields:
                raise ValueError("adb target accepts no SSH fields")


@dataclass
class PipelineConfig:
    """Platform-independent run configuration passed to every pipeline module."""

    model: str = ""
    claude_model: str = ""
    model_base_url: str = ""
    model_api_key: str = ""
    auth_token: str = ""
    api_mode: str = "native"
    vlm_key: str = ""
    vlm_model: str = ""
    vlm_base_url: str = ""
    agent_cli: str = "claude"
    model_max_retries: int = retry_policy.DEFAULT_MODEL_MAX_RETRIES
    context_1m: bool = False
    auto_compact_window: int = 0
    max_tokens_limit: int = 0
    thinking_effort: str = ""
    cua_preflight_mode: str = "strict"
    capture_tool_use_screenshots: bool = False
    stage: str = "recreation_eval"
    eval_target: str = "recreation"
    output_dir: str = "."
    target: TargetSpec = field(default_factory=TargetSpec)
    extra: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        stages_for(self.stage)
        if self.agent_cli not in ("claude", "codex"):
            raise ValueError(
                f"unknown agent_cli {self.agent_cli!r}; expected 'claude' or 'codex'"
            )
        self.model_max_retries = retry_policy.non_negative_int(
            self.model_max_retries, name="model_max_retries"
        )
        self.api_mode = normalize_api_mode(self.api_mode)
        self.context_1m = normalize_bool(self.context_1m, name="context_1m")
        self.auto_compact_window = retry_policy.non_negative_int(
            self.auto_compact_window, name="auto_compact_window"
        )
        self.max_tokens_limit = retry_policy.non_negative_int(
            self.max_tokens_limit, name="max_tokens_limit"
        )
        self.thinking_effort = str(self.thinking_effort or "").strip().lower()
        self.cua_preflight_mode = normalize_cua_preflight_mode(self.cua_preflight_mode)
        self.capture_tool_use_screenshots = normalize_bool(
            self.capture_tool_use_screenshots,
            name="capture_tool_use_screenshots",
        )
        self.eval_target = normalize_eval_target(self.eval_target)
        if self.eval_target == "reference" and self.stage != "eval":
            raise ValueError("eval_target='reference' requires stage='eval'")
        legacy_target_keys = sorted(LEGACY_TARGET_EXTRA_KEYS.intersection(self.extra))
        if legacy_target_keys:
            names = ", ".join(legacy_target_keys)
            raise ValueError(
                f"target fields are not accepted in PipelineConfig.extra: {names}; "
                "use target=TargetSpec(...) or the --host/--device CLI flags "
                "(including --host-key-path)"
            )
        legacy_provider_keys = sorted(
            LEGACY_PROVIDER_EXTRA_KEYS.intersection(self.extra)
        )
        if legacy_provider_keys:
            names = ", ".join(legacy_provider_keys)
            raise ValueError(
                f"provider controls are not accepted in PipelineConfig.extra: {names}; "
                "the caller must prepare the target before invoking RB"
            )
        legacy_runtime_keys = sorted(LEGACY_RUNTIME_EXTRA_KEYS.intersection(self.extra))
        if legacy_runtime_keys:
            names = ", ".join(legacy_runtime_keys)
            raise ValueError(
                f"runtime controls are not accepted in PipelineConfig.extra: {names}; "
                "use the matching PipelineConfig fields or rb run flags"
            )
        legacy_model_keys = sorted(LEGACY_MODEL_EXTRA_KEYS.intersection(self.extra))
        if legacy_model_keys:
            names = ", ".join(legacy_model_keys)
            raise ValueError(
                f"legacy model/provider fields are not accepted in "
                f"PipelineConfig.extra: {names}; use model, model_base_url, "
                "model_api_key and api_mode (or their matching rb run flags)"
            )


def target_spec(
    platform: str,
    *,
    host: str = "",
    user: str = "",
    password: str = "",
    port: int = 0,
    key_path: str = "",
    device: str = "",
) -> TargetSpec:
    """Build the platform-neutral descriptor for an already prepared target."""
    platform = (platform or "").strip().lower()
    if platform == "ubuntu":
        platform = "linux"
    host, device = (host or "").strip(), (device or "").strip()
    ssh_fields = bool(host or user or password or port or key_path)
    if platform == "web":
        if ssh_fields or device:
            raise ValueError(
                "web uses target.kind=local and accepts no SSH or ADB fields"
            )
        return TargetSpec(kind="local")
    if platform == "android":
        if ssh_fields:
            raise ValueError("android accepts an ADB device, not SSH fields")
        if not device:
            raise ValueError(
                "android requires --device; target provisioning belongs to the caller"
            )
        return TargetSpec(kind="adb", device=device)
    if platform in ("linux", "windows", "macos"):
        if device:
            raise ValueError(f"{platform} accepts --host, not --device")
        if not host:
            raise ValueError(
                f"{platform} requires --host; target provisioning belongs to the caller"
            )
        if platform == "windows" and key_path:
            raise ValueError(f"{platform} does not support --host-key-path")
        return TargetSpec(
            kind="ssh",
            host=host,
            user=user,
            password=password,
            port=int(port or 0),
            key_path=key_path,
        )
    raise ValueError(f"unknown platform {platform!r}; expected {', '.join(PLATFORMS)}")


def known_platforms() -> list[str]:
    return list(PLATFORMS)


def load_platform(platform: str) -> ModuleType:
    """Load and validate ``platforms.<platform>.pipeline`` directly."""
    name = (platform or "").strip().lower()
    if name == "ubuntu":
        name = "linux"
    if name not in PLATFORMS:
        raise ValueError(
            f"no pipeline for platform {platform!r} (known: {list(PLATFORMS)})"
        )
    module_name = f"platforms.{name}.pipeline"
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name in {module_name, f"platforms.{name}"}:
            raise ValueError(f"platform pipeline is missing: {module_name}") from exc
        raise
    if not callable(getattr(module, "run", None)):
        raise TypeError(f"{module_name} must define run(task, config)")
    declared = getattr(module, "PLATFORM", name)
    if declared != name:
        raise ValueError(
            f"{module_name} declares PLATFORM={declared!r}, expected {name!r}"
        )
    return module


def validate_normalized_state(state: object, *, platform: str, stage: str) -> dict:
    """Fail at the shared boundary when a platform returns a private state shape."""
    if not isinstance(state, dict):
        raise TypeError(
            f"{platform} pipeline returned {type(state).__name__}, expected dict"
        )
    stages = state.get("stages")
    if not isinstance(stages, dict):
        raise ValueError(f"{platform} pipeline returned no normalized stages mapping")
    missing = [name for name in stages_for(stage) if name not in stages]
    if missing:
        raise ValueError(f"{platform} pipeline omitted stage evidence: {missing}")
    if not isinstance(state.get("pipeline_exit"), int):
        raise ValueError(f"{platform} pipeline returned no integer pipeline_exit")
    evals = state.setdefault("evals", {})
    if not isinstance(evals, dict):
        raise ValueError(f"{platform} pipeline returned non-mapping evals")
    try:
        return exit_contract.validate_state_outcomes(state, stages_for(stage))
    except ValueError as exc:
        raise ValueError(
            f"{platform} pipeline returned invalid stage outcomes: {exc}"
        ) from exc


def load_task(args: argparse.Namespace) -> dict:
    """Build the release task identity.

    Source, reference and tests are resolved from the frozen unified artifact store
    instance by every platform pipeline. Accepting repo/commit/tasks here would
    create a second, mutable input path at the shared release boundary.
    """
    if args.task_id:
        return {"task_id": args.task_id}
    raise ValueError("need --task-id")


def parse_extra(pairs: list[str]) -> dict:
    """-x k=v pairs -> dict, with int/float/bool coercion."""
    out: dict = {}
    for p in pairs or []:
        if "=" not in p:
            raise ValueError(f"--extra expects key=value, got {p!r}")
        k, v = p.split("=", 1)
        lv = v.lower()
        if lv in ("true", "false"):
            out[k] = lv == "true"
        else:
            try:
                out[k] = int(v)
            except ValueError:
                try:
                    out[k] = float(v)
                except ValueError:
                    out[k] = v
    return out


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        # None falls back to basename(argv[0]). The `rb` console script sets RB_PROG so its
        # usage/error lines name the command a user typed, not this file.
        prog=os.environ.get("RB_PROG") or None,
        description="Unified Recreation-Bench pipeline (5 platforms)",
    )
    p.add_argument(
        "--platform",
        required=True,
        choices=PLATFORMS,
        help=f"target platform (known: {', '.join(known_platforms())})",
    )
    # Task identity also accepts an env fallback so launchers can keep it off argv.
    p.add_argument(
        "--task-id",
        default=os.environ.get("RB_TASK_ID"),
        help="task / app id (env: RB_TASK_ID)",
    )
    # common model wiring — one set of names for every platform
    p.add_argument("--model", default=os.environ.get("RB_MODEL", ""))
    p.add_argument(
        "--claude-model",
        default=os.environ.get("RB_CLAUDE_MODEL", ""),
        help=(
            "Claude Code client alias; upstream routing still uses --model "
            "(env: RB_CLAUDE_MODEL; default: claude-opus-4-8)"
        ),
    )
    p.add_argument("--model-base-url", default=os.environ.get("RB_MODEL_BASE_URL", ""))
    p.add_argument("--model-api-key", default=os.environ.get("RB_MODEL_API_KEY", ""))
    p.add_argument("--auth-token", default=os.environ.get("RB_AUTH_TOKEN", ""))
    p.add_argument(
        "--api-mode",
        choices=API_MODES,
        default=os.environ.get("RB_API_MODE", "native").strip().lower(),
        help=(
            "upstream protocol: native Anthropic Messages or OpenAI-compatible "
            "(env: RB_API_MODE; default: native)"
        ),
    )
    p.add_argument("--vlm-key", default=os.environ.get("RB_VLM_KEY", ""))
    p.add_argument("--vlm-model", default=os.environ.get("RB_VLM_MODEL", ""))
    p.add_argument("--vlm-base-url", default=os.environ.get("RB_VLM_BASE_URL", ""))
    p.add_argument(
        "--agent-cli",
        choices=("claude", "codex"),
        default=os.environ.get("RB_AGENT_CLI")
        or os.environ.get("AGENT_CLI")
        or "claude",
        help="agent runtime shared by all platforms (env: RB_AGENT_CLI or AGENT_CLI)",
    )
    p.add_argument(
        "--model-max-retries",
        type=int,
        default=int(
            os.environ.get("RB_MODEL_MAX_RETRIES")
            or os.environ.get("MODEL_MAX_RETRIES")
            or retry_policy.DEFAULT_MODEL_MAX_RETRIES
        ),
        help=(
            "extra transient retries for each model API request, owned by the shared "
            "proxy (env: RB_MODEL_MAX_RETRIES or MODEL_MAX_RETRIES; default: 10)"
        ),
    )
    p.add_argument(
        "--context-1m",
        type=lambda value: normalize_bool(value, name="context_1m"),
        default=os.environ.get("RB_CONTEXT_1M", "false"),
        metavar="BOOL",
        help="enable the extended model context (env: RB_CONTEXT_1M)",
    )
    p.add_argument(
        "--auto-compact-window",
        type=int,
        default=int(os.environ.get("RB_AUTO_COMPACT_WINDOW", "0") or 0),
        help="agent auto-compaction window; 0 keeps its default (env: RB_AUTO_COMPACT_WINDOW)",
    )
    p.add_argument(
        "--max-tokens-limit",
        type=int,
        default=int(os.environ.get("RB_MAX_TOKENS_LIMIT", "0") or 0),
        help="maximum model output tokens; 0 keeps its default (env: RB_MAX_TOKENS_LIMIT)",
    )
    p.add_argument(
        "--thinking-effort",
        default=os.environ.get("RB_THINKING_EFFORT", ""),
        help="model reasoning effort (env: RB_THINKING_EFFORT)",
    )
    p.add_argument(
        "--cua-preflight-mode",
        choices=CUA_PREFLIGHT_MODES,
        default=os.environ.get("RB_CUA_PREFLIGHT_MODE", "strict").strip().lower(),
        help=(
            "reference MCP preflight policy shared by all platforms "
            "(env: RB_CUA_PREFLIGHT_MODE; default: strict)"
        ),
    )
    p.add_argument(
        "--capture-tool-use-screenshots",
        type=lambda value: normalize_bool(value, name="capture_tool_use_screenshots"),
        default=os.environ.get("RB_CAPTURE_TOOL_USE_SCREENSHOTS", "false"),
        metavar="BOOL",
        help=(
            "capture the live desktop after every completed recreation tool use; "
            "failures are diagnostic only "
            "(env: RB_CAPTURE_TOOL_USE_SCREENSHOTS; default: false)"
        ),
    )
    # One lifecycle field everywhere. ``recreation_eval`` is the complete release;
    # setup is a standalone permission-boundary diagnostic.
    p.add_argument(
        "--stage",
        choices=STAGE_VALUES,
        default=os.environ.get("RB_STAGE", "recreation_eval"),
    )
    p.add_argument(
        "--eval-target",
        choices=EVAL_TARGETS,
        default=os.environ.get("RB_EVAL_TARGET", "recreation"),
        help=(
            "candidate graded by an eval-only run: recreation restores a prior "
            "run, reference grades the frozen reference app (env: RB_EVAL_TARGET)"
        ),
    )
    p.add_argument(
        "--output-dir",
        default=os.environ.get("RB_OUTPUT_DIR", "."),
        help="where metrics.json is written",
    )
    # The runtime provider prepares the target before invoking RB. These arguments build the
    # only target descriptor; platform-native spellings are private worker translations and
    # must never be accepted through --extra as a second source of truth.
    p.add_argument(
        "--host",
        default=os.environ.get("RB_HOST", ""),
        help="prepared SSH target address (required for desktop platforms)",
    )
    p.add_argument("--host-user", default=os.environ.get("RB_HOST_USER", ""))
    p.add_argument("--host-password", default=os.environ.get("RB_HOST_PASSWORD", ""))
    p.add_argument(
        "--host-port", type=int, default=int(os.environ.get("RB_HOST_PORT", "0") or 0)
    )
    p.add_argument(
        "--host-key-path",
        default=os.environ.get("RB_HOST_KEY_PATH", ""),
        help="SSH private-key path on the RB controller (Linux or macOS targets)",
    )
    p.add_argument(
        "--device",
        default=os.environ.get("RB_DEVICE") or os.environ.get("ANDROID_SERIAL", ""),
        help="prepared Android ADB serial (for example emulator-5554)",
    )
    p.add_argument(
        "-x",
        "--extra",
        action="append",
        default=[],
        help="platform-specific knob key=value (vm_port, image, backend, *_timeout)",
    )
    return p


def config_from_args(args: argparse.Namespace) -> PipelineConfig:
    target = target_spec(
        getattr(args, "platform", ""),
        host=getattr(args, "host", "") or "",
        user=getattr(args, "host_user", "") or "",
        password=getattr(args, "host_password", "") or "",
        port=getattr(args, "host_port", 0) or 0,
        key_path=getattr(args, "host_key_path", "") or "",
        device=getattr(args, "device", "") or "",
    )
    return PipelineConfig(
        model=args.model,
        claude_model=args.claude_model,
        model_base_url=args.model_base_url,
        model_api_key=args.model_api_key,
        auth_token=args.auth_token,
        api_mode=args.api_mode,
        vlm_key=args.vlm_key,
        vlm_model=args.vlm_model,
        vlm_base_url=args.vlm_base_url,
        agent_cli=args.agent_cli,
        model_max_retries=args.model_max_retries,
        context_1m=args.context_1m,
        auto_compact_window=args.auto_compact_window,
        max_tokens_limit=args.max_tokens_limit,
        thinking_effort=args.thinking_effort,
        cua_preflight_mode=args.cua_preflight_mode,
        capture_tool_use_screenshots=args.capture_tool_use_screenshots,
        stage=args.stage,
        eval_target=args.eval_target,
        output_dir=args.output_dir,
        target=target,
        extra=parse_extra(args.extra),
    )


def required_stages_for(stage: str, required: tuple[str, ...] | list[str]) -> list[str]:
    """Return the required release stages reached by one canonical stage token."""
    selected = set(stages_for(stage))
    return [name for name in required if name in selected]


def read_target_info(output_dir: str | Path) -> dict:
    """Read optional metadata written by the caller that prepared the target."""
    path = Path(output_dir) / "sandbox_info.json"
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError, TypeError):
        return {}
    return value if isinstance(value, dict) else {}


def _finalize_metrics(
    args: argparse.Namespace,
    metrics: dict,
    output_path: Path,
    *,
    platform=None,
    cfg: PipelineConfig | None = None,
) -> None:
    """Run a platform's best-effort artifact finalizer for every terminal path."""
    try:
        platform = platform or load_platform(args.platform)
        finalize = getattr(platform, "finalize", None)
        if callable(finalize):
            finalize(metrics, cfg or config_from_args(args), output_path)
    except (
        Exception
    ) as exc:  # Final artifact transport must not replace the run result.
        print(f"[{args.platform}] WARN: finalization failed: {exc}")


def run(args: argparse.Namespace) -> dict:
    """Load one platform pipeline, run it, and assemble unified metrics."""
    task = load_task(args)
    cfg = config_from_args(args)
    platform = load_platform(args.platform)

    state = validate_normalized_state(
        platform.run(task, cfg), platform=args.platform, stage=cfg.stage
    )
    state.setdefault("task_id", task.get("task_id", ""))
    state.setdefault("model", cfg.model)
    target_info = read_target_info(args.output_dir)
    if target_info:
        state.setdefault("sandbox_info", target_info)

    # One shared metrics terminus: honor each platform's own task_score override
    # while emitting the one contract.
    metrics = metrics_contract.assemble_metrics(
        state,
        platform=args.platform,
        pass_rule=getattr(platform, "PASS_RULE", "exit_and_stages"),
        required_stages=required_stages_for(cfg.stage, RELEASE_STAGES),
        stage=cfg.stage,
        sandbox_info=state.get("sandbox_info"),
    )
    out = Path(args.output_dir) / "metrics.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(metrics, indent=2))
    _finalize_metrics(args, metrics, out, platform=platform, cfg=cfg)
    return metrics


def _failure_metrics(args: argparse.Namespace, exc: BaseException, *, rc: int) -> dict:
    """Write the same envelope when the shared controller cannot return state."""
    from core.inpod import failed_state

    selected = list(stages_for(args.stage))
    state = failed_state(
        args.task_id or "",
        args.model,
        selected,
        rc,
        f"shared pipeline exception: {type(exc).__name__}: {exc}",
    )
    metrics = metrics_contract.assemble_metrics(
        state,
        platform=args.platform,
        required_stages=required_stages_for(args.stage, RELEASE_STAGES),
        stage=args.stage,
        sandbox_info=read_target_info(args.output_dir),
    )
    out = Path(args.output_dir) / "metrics.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(metrics, indent=2))
    _finalize_metrics(args, metrics, out)
    return metrics


def _exception_metrics(args: argparse.Namespace, exc: Exception) -> dict:
    return _failure_metrics(args, exc, rc=exit_contract.RC_INFRA)


def main() -> int:
    args = build_parser().parse_args()
    old_handlers: dict[int, object] = {}

    def terminate(signum, _frame) -> None:
        raise PipelineTerminated(signum)

    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            old_handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, terminate)
        try:
            metrics = run(args)
        except PipelineTerminated as exc:
            metrics = _failure_metrics(args, exc, rc=exit_contract.RC_TIMEOUT)
        except SystemExit as exc:
            # Platform adapters are libraries at this boundary. A stray native
            # sys.exit must not bypass metrics emission or leak a private code.
            native = exc.code if isinstance(exc.code, int) else None
            native_or_public = (
                exit_contract.RC_TIMEOUT
                if native == exit_contract.RC_TIMEOUT
                else native or exit_contract.RC_INFRA
            )
            traceback.print_exc()
            metrics = _failure_metrics(args, exc, rc=native_or_public)
        except Exception as exc:
            traceback.print_exc()
            metrics = _exception_metrics(args, exc)
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
    print(json.dumps(metrics, indent=2))
    passed = bool(metrics.get("passed"))
    if not passed:
        # One diagnosis for all five platforms, because the failure is not one platform's:
        # a model-gateway content-safety error can invalidate an entire platform batch.
        # Without this the operator sees only a local timeout and reads it as a network fault.
        # Best-effort: gateway diagnostics never mask the platform failure.
        from infrastructure.model_gateway import diagnostics

        diag = diagnostics.diagnose_dir(args.output_dir)
        if diag:
            print(diag)
    return exit_contract.process_exit_code(metrics)


if __name__ == "__main__":
    raise SystemExit(main())
