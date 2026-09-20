#!/usr/bin/env python3
"""Shared launcher for native workers called by platform pipelines."""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path

from core import agent_invocation, exit_contract
from core.pipeline import PipelineConfig


def worker_env(
    task: dict,
    cfg: PipelineConfig,
    base_env: dict[str, str] | None = None,
) -> dict[str, str]:
    """Translate the shared pipeline config into one worker environment.

    Platform workers predate :mod:`core.pipeline` and still consume a handful
    of platform-native names. Keeping this translation here makes direct CLI
    invocation equivalent to the deployment adapter path and overwrites stale ambient
    values instead of accidentally using another model or judge configuration.

    Secrets remain environment-only; callers must not copy these values into
    worker argv.
    """
    task_id = str(task.get("task_id") or "")
    model_key = cfg.model_api_key or cfg.auth_token
    claude_model = agent_invocation.claude_model_alias(cfg.claude_model)
    client_model = cfg.model if cfg.agent_cli == "codex" else claude_model
    context_1m = "true" if cfg.context_1m else "false"
    compact_window = str(cfg.auto_compact_window or "")
    max_tokens_limit = str(cfg.max_tokens_limit or "")
    values = {
        # Canonical RB boundary.
        "RB_TASK_ID": task_id,
        "RB_MODEL": cfg.model,
        "RB_CLAUDE_MODEL": claude_model,
        "RB_MODEL_BASE_URL": cfg.model_base_url,
        "RB_MODEL_API_KEY": cfg.model_api_key,
        "RB_AUTH_TOKEN": cfg.auth_token,
        "RB_API_MODE": cfg.api_mode,
        "RB_VLM_KEY": cfg.vlm_key,
        "RB_VLM_MODEL": cfg.vlm_model,
        "RB_VLM_BASE_URL": cfg.vlm_base_url,
        "RB_AGENT_CLI": cfg.agent_cli,
        "RB_MODEL_MAX_RETRIES": str(cfg.model_max_retries),
        "RB_CONTEXT_1M": context_1m,
        "RB_AUTO_COMPACT_WINDOW": compact_window,
        "RB_MAX_TOKENS_LIMIT": max_tokens_limit,
        "RB_THINKING_EFFORT": cfg.thinking_effort,
        "RB_CUA_PREFLIGHT_MODE": cfg.cua_preflight_mode,
        "RB_CAPTURE_TOOL_USE_SCREENSHOTS": (
            "true" if cfg.capture_tool_use_screenshots else "false"
        ),
        "RB_STAGE": cfg.stage,
        "RB_EVAL_TARGET": cfg.eval_target,
        "RB_OUTPUT_DIR": cfg.output_dir,
        "RB_TARGET_KIND": cfg.target.kind,
        "RB_HOST": cfg.target.host,
        "RB_HOST_USER": cfg.target.user,
        "RB_HOST_PASSWORD": cfg.target.password,
        "RB_HOST_PORT": str(cfg.target.port or 22) if cfg.target.host else "",
        "RB_HOST_KEY_PATH": cfg.target.key_path,
        "RB_DEVICE": cfg.target.device,
        "ANDROID_SERIAL": cfg.target.device,
        "DEVICE_ID": cfg.target.device,
        # Native names still read by the five existing workers.
        "TASK_ID": task_id,
        "INSTANCE_ID": task_id,
        "MODEL": cfg.model,
        "CLAUDE_MODEL": claude_model,
        "ANTHROPIC_MODEL": client_model,
        "MODEL_BASE_URL": cfg.model_base_url,
        "ANTHROPIC_BASE_URL": cfg.model_base_url,
        "MODEL_API_KEY": cfg.model_api_key,
        "ANTHROPIC_API_KEY": model_key,
        "ANTHROPIC_AUTH_TOKEN": cfg.auth_token,
        "API_MODE": cfg.api_mode,
        "VLM_API_KEY": cfg.vlm_key,
        "VLM_MODEL_API_KEY": cfg.vlm_key,
        "VLM_JUDGE_API_KEY": cfg.vlm_key,
        "VLM_MODEL": cfg.vlm_model,
        "VLM_JUDGE_MODEL": cfg.vlm_model,
        "VLM_BASE_URL": cfg.vlm_base_url,
        "VLM_MODEL_BASE_URL": cfg.vlm_base_url,
        "VLM_JUDGE_BASE_URL": cfg.vlm_base_url,
        "AGENT_CLI": cfg.agent_cli,
        "MODEL_MAX_RETRIES": str(cfg.model_max_retries),
        "CONTEXT_1M": context_1m,
        "AUTO_COMPACT_WINDOW": compact_window,
        "MAX_TOKENS_LIMIT": max_tokens_limit,
        "THINKING_EFFORT": cfg.thinking_effort,
        # Temporary worker-native aliases. They are derived here, never read as
        # inputs, so old workers cannot create a second configuration source.
        "CLAUDE_CODE_AUTO_COMPACT_WINDOW": compact_window,
        "CLAUDE_CODE_MAX_OUTPUT_TOKENS": max_tokens_limit,
        "RB_CLAUDE_CODE_AUTO_COMPACT_WINDOW": compact_window,
        "RB_CLAUDE_CODE_MAX_OUTPUT_TOKENS": max_tokens_limit,
        "RB_MAX_OUTPUT_TOKENS": max_tokens_limit,
        # The proxy owns transient per-request retry. Relaunching an agent process here
        # would multiply attempts and give different CLIs different effective budgets.
        "CLAUDE_CODE_MAX_RETRIES": "0",
        "STAGE": cfg.stage,
        "EVAL_TARGET": cfg.eval_target,
        "OUTPUT_DIR": cfg.output_dir,
    }
    env = dict(os.environ if base_env is None else base_env)
    # These were once public protocol inputs. Deployment adapters may still derive
    # compatibility values, but native RB workers must not inherit stale values that
    # can disagree with the canonical field.
    for name in ("USE_NATIVE_ANTHROPIC", "CUSTOM_LLM_PROVIDER"):
        env.pop(name, None)
    env.update({name: str(value or "") for name, value in values.items()})
    return env


def run_worker(
    runner: str | os.PathLike[str],
    cfg: PipelineConfig,
    *,
    env: dict[str, str] | None = None,
    interpreter: str = "bash",
    args: list[str] | None = None,
) -> int:
    """Run one native harness in this pod and forward termination signals.

    The runner is passed as an argv element, never evaluated as shell text.  Its
    existing stdout/stderr remain the deployment platform job log.  SIGTERM forwarding preserves
    the harness' artifact/metrics flush when the pod reaches its deadline.
    """
    path = Path(runner).resolve()
    if not path.is_file():
        raise RuntimeError(f"in-pod worker does not exist: {path}")

    child_env = os.environ.copy()
    child_env.update(
        {
            "RB_PIPELINE_WORKER": "1",
            "STAGE": cfg.stage,
        }
    )
    if env:
        child_env.update({k: str(v) for k, v in env.items()})

    proc = subprocess.Popen(
        [interpreter, str(path), *(args or [])],
        env=child_env,
        start_new_session=True,
    )

    old_handlers: dict[int, object] = {}
    termination_requested = False

    def forward(signum, _frame) -> None:
        nonlocal termination_requested
        termination_requested = True
        try:
            os.killpg(proc.pid, signum)
        except ProcessLookupError:
            pass

    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            old_handlers[sig] = signal.getsignal(sig)
            signal.signal(sig, forward)
        rc = proc.wait()
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)

    if termination_requested or rc in (-signal.SIGTERM, -signal.SIGINT):
        return exit_contract.RC_TIMEOUT
    return 128 + abs(rc) if rc < 0 else rc


def failed_state(
    task_id: str,
    model: str,
    stages: list[str],
    rc: int,
    message: str,
    *,
    outcome_class: str | None = None,
    reason_code: str | None = None,
) -> dict:
    """Return a contract-shaped failure when a worker produced no native result.

    Most missing results are infrastructure failures.  A platform whose worker
    already has the shared 0/1/2/143 contract may pass its explicit outcome so
    deterministic input failures are not rewritten as retryable infrastructure.
    """
    native_exit = rc
    effective_exit = rc or exit_contract.RC_INFRA
    inferred_outcome = (
        exit_contract.validate_outcome_class(outcome_class)
        if outcome_class is not None
        else exit_contract.from_native_exit(
            effective_exit, one=exit_contract.OUTCOME_INFRA_ERROR
        )
    )
    canonical_reason = exit_contract.normalize_reason_code(
        reason_code or message, fallback="worker_result_missing"
    )
    state = {
        "task_id": task_id,
        "model": model,
        "stages": {
            stage: (
                exit_contract.STAGE_FAIL if index == 0 else exit_contract.STAGE_NOT_RUN
            )
            for index, stage in enumerate(stages)
        },
        "evals": {},
        "pipeline_exit": effective_exit,
        "native_exit_code": native_exit,
        "summary": {"error": message},
    }
    outcomes = {}
    for index, stage in enumerate(stages):
        if index == 0:
            status = (
                exit_contract.STAGE_TIMEOUT
                if inferred_outcome == exit_contract.OUTCOME_TERMINATED
                else (
                    exit_contract.STAGE_FAIL
                    if inferred_outcome == exit_contract.OUTCOME_DATA_ERROR
                    else exit_contract.STAGE_ERROR
                )
            )
            stage_reason = canonical_reason
        else:
            status = exit_contract.STAGE_NOT_RUN
            stage_reason = "upstream_stage_failed"
        outcomes[stage] = exit_contract.stage_outcome(
            status=status,
            outcome_class=inferred_outcome,
            reason_code=stage_reason,
            native_exit_code=native_exit if index == 0 else None,
        )
    return exit_contract.apply_stage_outcomes(state, outcomes, stages)
