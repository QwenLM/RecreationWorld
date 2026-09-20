"""Stage: Recreation -- agent reverse-engineers a frozen reference build.

The pipeline first checks out the descriptor's pinned commit and recursively materializes
submodules, falling back to the packaged source tree when that cannot be done.  The frozen
reference recipe then builds and launches that exact source for the agent to explore.

Runs directly on the VM (no Docker container).  Requires vm_bootstrap to have
run first so that build tools, Xvfb, D-Bus, openbox, and Claude Code are
available.
"""

from __future__ import annotations

import base64 as b64mod
import json
import os
import shlex
import time
from pathlib import Path

from vm_utils import run_command, upload_to_vm

try:
    from core import (
        agent_config,
        agent_invocation,
        mcp_settings,
        permission_probe,
        recreation_artifact,
        recreation_prompt,
        runtime_assets,
        tool_use_capture,
        trajectory,
    )
    from core import (  # shared across all platforms: MCP wiring + collector + locator
        cua_driver as core_cua_driver,
    )
except ImportError:  # legacy entry may not have scripts/ on sys.path
    import os as _os
    import sys as _sys

    _sys.path.insert(
        0,
        _os.path.dirname(
            _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__)))
        ),
    )
    from core import (
        agent_config,
        agent_invocation,
        mcp_settings,
        permission_probe,
        recreation_artifact,
        recreation_prompt,
        runtime_assets,
        tool_use_capture,
        trajectory,
    )
from core import scope as core_scope  # noqa: E402
from core.pipeline import normalize_cua_preflight_mode  # noqa: E402

from stages.config import (
    CODEX_CONFIG_TOML_TEMPLATE,
    VM_CODE_DIR,
    codex_model_slug,
    vm_task_dir,
)
from stages.runtime_helpers import RUNTIME_HELPER_FILES

# Settings come from the ONE shared contract (core/agent_config.py). This dict WAS the
# reference the other four platforms are now aligned to; a private copy here is exactly how
# they drifted apart, and `deny` is the half that actually takes effect.
_SETTINGS_B64 = agent_config.claude_settings_b64(
    mcp_servers=(mcp_settings.DESKTOP_SERVER,),
    # RecreationBench Desktop grants the disposable GUI session, including full-display capture.
    # DESKTOP_SESSION therefore keeps get_desktop_state and set_config available; the latter is
    # needed because qwen-cua-driver starts in its narrower capture_scope=window mode.
    scope=core_scope.DESKTOP_SESSION,
)

SCRIPT = runtime_assets.load_text("linux/runtime/recreation.sh")
MODEL_PROXY_SOURCE = runtime_assets.path("linux/runtime/model_proxy.py")


def run(
    client,
    task: dict,
    instance_id: str,
    model: str,
    image: str,
    api_key: str,
    base_url: str,
    auth_token: str = "",
    timeout: int = 77400,
    mcp_provider: str = "cua-driver",
    agent_cli: str = "claude",
    reference_build_only: bool = False,
) -> str:
    base = vm_task_dir(task["task_id"])
    model_slug = model.replace(".", "-")
    rec_dir = f"{base}/recreation_{model_slug}"

    run_command(client, f"mkdir -p {rec_dir}", instance_id=instance_id, timeout=15)
    time.sleep(2)

    # Ensure the optional frozen-fixtures directory exists for the workspace copy.
    run_command(
        client,
        f"mkdir -p {base}/tests/fixtures",
        instance_id=instance_id,
        timeout=15,
    )

    # Every codex spelling uses desktop-control MCP. ``codex-mcp`` remains accepted as a
    # backwards-compatible alias; there is no longer a CLI-only observation path.
    codex_mcp = agent_cli.startswith("codex")
    norm_cli = "codex" if agent_cli.startswith("codex") else agent_cli
    claude_model = agent_invocation.claude_model_alias(
        os.environ.get("CLAUDE_MODEL", "")
    )
    if codex_mcp and mcp_provider != "cua-driver":
        raise ValueError(
            "Codex desktop recreation requires the cua-driver MCP provider"
        )

    prompt = recreation_prompt.render("linux")

    upload_to_vm(client, prompt, f"{rec_dir}/prompt.txt", instance_id=instance_id)
    for name, content in RUNTIME_HELPER_FILES.items():
        upload_to_vm(
            client,
            content,
            f"{rec_dir}/{name}",
            instance_id=instance_id,
        )
    upload_to_vm(
        client,
        MODEL_PROXY_SOURCE.read_text(encoding="utf-8"),
        f"{rec_dir}/model_proxy.py",
        instance_id=instance_id,
    )

    if mcp_provider == "cua-driver":
        command, args = "cua-driver", ["mcp", "--no-overlay"]
        # linux was the only platform seeding NO daemon env here, so the driver was free to
        # self-upgrade mid-benchmark. The coordinate space is derived the same two ways
        # vm_bootstrap derives it (RB_CUA_COORDINATE_SPACE, or a `:norm`/`+norm` suffix on
        # RB_CUA_DRIVER_REF) rather than defaulting to 0: when coord=1 the bootstrap installs a
        # wrapper that re-exports SPACE=1, so a hardcoded 0 here would still WORK -- the
        # wrapper's own export wins over the inherited value -- but the config would then state
        # the opposite of what the driver does, which is a trap for the next reader and breaks
        # silently if the wrapper ever stops re-exporting.
        _, _, _ref_norm = core_cua_driver.parse_ref(
            os.environ.get("RB_CUA_DRIVER_REF", "")
        )
        _normalize = _ref_norm or os.environ.get("RB_CUA_COORDINATE_SPACE", "") == "1"
        mcp_config = json.dumps(
            mcp_settings.mcp_config(
                mcp_settings.DESKTOP_SERVER,
                command,
                args,
                mcp_settings.desktop_env(normalize=_normalize),
            )
        )
    else:
        # computer-use-mcp is a different server binary; the CUA_DRIVER_RS_* contract does not
        # apply to it, so it gets the entry shape but no driver env.
        mcp_config = json.dumps(
            mcp_settings.mcp_config(mcp_settings.DESKTOP_SERVER, "computer-use-mcp", [])
        )

    mcp_config_b64 = b64mod.b64encode(mcp_config.encode()).decode()

    # Codex needs the bare slug in its config for correct metadata. Any provider
    # model-name translation belongs to the deployment-owned endpoint.
    codex_slug = codex_model_slug(model)
    codex_config = CODEX_CONFIG_TOML_TEMPLATE.format(
        model=codex_slug,
        base_url=base_url,
    )
    # Codex reasoning effort: reuse the already-forwarded thinking_effort param
    # (THINKING_EFFORT env). Valid efforts are MODEL-DEPENDENT: every codex model takes
    # minimal..xhigh; gpt-5.6-sol additionally supports max/ultra (its registry lists
    # low(default)/medium/high/xhigh/max/ultra). Older codex models (e.g. mr.gpt-5.5) reject
    # max/ultra — writing them makes config.toml invalid — so those stay excluded there, and an
    # out-of-set value (e.g. the "max" default on gpt-5.5) omits the line → codex keeps its default.
    _codex_effort = os.environ.get("THINKING_EFFORT", "").strip().lower()
    _codex_valid = {"minimal", "low", "medium", "high", "xhigh"}
    if "gpt-5.6" in codex_slug:
        _codex_valid |= {"max", "ultra"}
    if norm_cli == "codex" and _codex_effort in _codex_valid:
        # These MUST be TOP-LEVEL keys, inserted BEFORE the first [model_providers.*]
        # table. The template ends with that table, so APPENDING here would make TOML
        # nest them UNDER the provider table → codex reads top-level model_reasoning_effort
        # as absent → silently runs at its default (low), summary none. VM-confirmed
        # 2026-07-25 via local codex 0.142.3 + 0.145.0 capture: append-after-table →
        # "reasoning effort: none" / sends effort=low; insert-before-table → effort honored.
        # Gate `model_supports_reasoning_summaries` is required for mr.gpt-5.5 (else codex
        # sends reasoning:null); harmless for gpt-5.6.
        _eff_lines = (
            f'model_reasoning_effort = "{_codex_effort}"\n'
            "model_supports_reasoning_summaries = true\n"
        )
        _ins = codex_config.find("[model_providers")
        codex_config = (
            codex_config[:_ins] + _eff_lines + codex_config[_ins:]
            if _ins != -1
            else codex_config + _eff_lines
        )
    if codex_mcp:
        codex_mcp_timeout_sec = (
            int(
                mcp_settings.tool_timeout_ms(
                    os.environ.get(mcp_settings.TOOL_TIMEOUT_OVERRIDE_VAR)
                )
            )
            // 1000
        )
        codex_config += agent_config.mcp_server_toml(
            mcp_settings.DESKTOP_SERVER,
            "cua-driver",
            ["mcp", "--no-overlay"],
            startup_timeout_sec=60,
            tool_timeout_sec=codex_mcp_timeout_sec,
        )
    codex_config_b64 = b64mod.b64encode(codex_config.encode()).decode()

    collect_sessions = (
        "python3 /tmp/trajectory.py collect "
        "--projects /home/user/.claude/projects:user,/home/user/.codex/sessions:user "
        "--dest /workspace/output/sessions --since 0 --stage recreation "
        "--stream /workspace/output/trajectory.jsonl"
    )
    _rec_manifest = json.dumps(
        recreation_artifact.manifest("ubuntu", "."), ensure_ascii=False
    )
    # The platform pipeline passes one provider-neutral endpoint. The root relay
    # below keeps it behind VM loopback for Linux's uid-scoped isolation.
    codex_endpoint_url = base_url if norm_cli == "codex" else ""
    script = runtime_assets.render_text(
        "linux/runtime/recreation.sh",
        {
            "__REC_DIR__": rec_dir,
            "__BASE__": base,
            "__RB_CODE_DIR__": VM_CODE_DIR,
            "__MODEL__": model,
            "__CLAUDE_MODEL__": claude_model,
            "__API_KEY__": api_key,
            "__AUTH_TOKEN__": auth_token,
            "__BASE_URL__": base_url,
            "__SETTINGS__": _SETTINGS_B64,
            "__RUN_ID__": task["task_id"],
            "__MCP_CONFIG_B64__": mcp_config_b64,
            "__MCP_TOOL_TIMEOUT_EXPORT__": mcp_settings.tool_timeout_export_sh(),
            "__AGENT_CLI__": norm_cli,
            "__COLLECT_SESSIONS__": collect_sessions,
            "__WRITE_REC_MANIFEST__": (
                f"    cat > /workspace/recreation/{recreation_artifact.MANIFEST_NAME} "
                f"<<'RBRECMF' || true\n{_rec_manifest}\nRBRECMF"
            ),
            "__CODEX_CONFIG_B64__": codex_config_b64,
            "__CODEX_ENDPOINT_URL__": codex_endpoint_url,
        },
    )

    sudo_password = os.environ.get("RB_SSH_PASSWORD", "")
    thinking_effort = os.environ.get("THINKING_EFFORT", "")
    env_prefix = ""
    if sudo_password:
        env_prefix += f"export RB_SUDO_PASSWORD={shlex.quote(sudo_password)}\n"
    if thinking_effort:
        env_prefix += f"export RB_THINKING_EFFORT={shlex.quote(thinking_effort)}\n"
    cua_preflight_mode = normalize_cua_preflight_mode(
        os.environ.get("RB_CUA_PREFLIGHT_MODE")
    )
    env_prefix += "export RB_CUA_PREFLIGHT_MODE=" f"{shlex.quote(cua_preflight_mode)}\n"
    env_prefix += (
        "export RB_CAPTURE_TOOL_USE_SCREENSHOTS="
        + shlex.quote(os.environ.get("RB_CAPTURE_TOOL_USE_SCREENSHOTS", "false"))
        + "\n"
    )
    context_1m = os.environ.get("CONTEXT_1M", "")
    if context_1m:
        env_prefix += f"export RB_CONTEXT_1M={shlex.quote(context_1m)}\n"
    auto_compact_window = os.environ.get("AUTO_COMPACT_WINDOW", "").strip()
    if auto_compact_window in ("", "0"):
        # The recreation-bench-linux deployment platform template pins AUTO_COMPACT_WINDOW=0 (it does not
        # forward the submitted job param), so a real value never reaches here. Default it
        # ON: auto-compaction is the fix for screenshot-heavy context overflow (input grew
        # to ~200k, and input + max_tokens then exceeds the 262144 limit). The trigger sits
        # ~15-33k below the window, so 200000 (~167k trigger) keeps input well under 262144;
        # 1M-context models get 400000.
        auto_compact_window = (
            "400000" if context_1m.lower() in ("1", "true", "yes") else "200000"
        )
    env_prefix += f"export RB_AUTO_COMPACT_WINDOW={shlex.quote(auto_compact_window)}\n"
    # Inner agent wall-clock budget — default 20h (72000s). Raised by EITHER
    # RB_RECREATION_AGENT_TIMEOUT (explicit) OR the recreation_timeout param
    # (RECREATION_TIMEOUT env) when that exceeds 20h — so slow models can be de-timeouted
    # through the standard param without a template change. pipeline.py derives the outer
    # RunCommand wall from this value plus its post-agent allowance.
    agent_timeout = str(
        max(
            int(os.environ.get("RB_RECREATION_AGENT_TIMEOUT") or 72000),
            int(os.environ.get("RECREATION_TIMEOUT") or 0),
        )
    )
    env_prefix += f"export RB_RECREATION_AGENT_TIMEOUT={shlex.quote(agent_timeout)}\n"
    # These two clocks have different units and meanings. Carry both explicitly across
    # the pod -> VM boundary so direct and provisioned runs use the same policy.
    api_timeout_ms = os.environ.get("RB_API_TIMEOUT_MS", "1800000").strip() or "1800000"
    env_prefix += f"export RB_API_TIMEOUT_MS={shlex.quote(api_timeout_ms)}\n"
    mcp_timeout_ms = os.environ.get("RB_MCP_TOOL_TIMEOUT", "").strip()
    if mcp_timeout_ms:
        env_prefix += "export RB_MCP_TOOL_TIMEOUT=" f"{shlex.quote(mcp_timeout_ms)}\n"
    effective_mcp_timeout_ms = int(mcp_timeout_ms or mcp_settings.tool_timeout_ms(None))
    # Eval executes inside the VM, not in the deployment platform pod where these profile parameters
    # originate. Preserve the judge retry/rate-limit policy across that boundary.
    for name in (
        "RB_VLM_JUDGE_RETRIES",
        "RB_VLM_JUDGE_MIN_INTERVAL",
        "RB_VLM_JUDGE_BATCH_SIZE",
    ):
        value = os.environ.get(name, "").strip()
        if value:
            env_prefix += f"export {name}={shlex.quote(value)}\n"
    max_out = os.environ.get("RB_MAX_OUTPUT_TOKENS", "")
    if max_out:
        env_prefix += f"export RB_MAX_OUTPUT_TOKENS={shlex.quote(max_out)}\n"
    # Extra request-body JSON is passed to CLAUDE_CODE_EXTRA_BODY on the target.
    # It is sourced from the controller's EXTRA_BODY/RB_EXTRA_BODY environment.
    extra_body = os.environ.get("EXTRA_BODY", "") or os.environ.get("RB_EXTRA_BODY", "")
    if extra_body.strip():
        env_prefix += f"export RB_EXTRA_BODY={shlex.quote(extra_body)}\n"
    if reference_build_only:
        env_prefix += "export RB_REFERENCE_BUILD_ONLY=1\n"

    invocation = agent_invocation.InvocationSpec(
        agent_cli=norm_cli,
        model=(
            codex_slug
            if norm_cli == "codex"
            else agent_config.claude_code_model(
                claude_model,
                context_1m=context_1m.lower() in ("1", "true", "yes", "on"),
            )
        ),
        prompt_file="/tmp/rb_prompt.txt",
        workspace="/workspace/recreation",
        trajectory_path="/workspace/output/trajectory.jsonl",
        stderr_path="/workspace/output/stderr.log",
        timeout_sec=int(agent_timeout),
        request_timeout_ms=int(api_timeout_ms),
        tool_timeout_ms=effective_mcp_timeout_ms,
        mcp_config=("" if norm_cli == "codex" else "/home/user/mcp_config.json"),
        reasoning_effort=(
            _codex_effort
            if norm_cli == "codex" and _codex_effort in _codex_valid
            else (thinking_effort or "high" if norm_cli != "codex" else "")
        ),
        run_name="rb-recreation-anonymous",
        capture_tool_use_screenshots=os.environ.get(
            "RB_CAPTURE_TOOL_USE_SCREENSHOTS", "false"
        )
        .strip()
        .lower()
        in ("1", "true", "yes", "on"),
        screenshot_dir="/workspace/output/tool_use_screenshots",
        screenshot_command=("python3", "/tmp/desktop_capture.py", "{output}"),
    )
    upload_to_vm(
        client,
        agent_invocation.spec_json(invocation),
        f"{rec_dir}/invocation.json",
        instance_id=instance_id,
    )
    upload_to_vm(
        client,
        Path(agent_invocation.__file__).read_text(encoding="utf-8"),
        f"{rec_dir}/agent_invocation.py",
        instance_id=instance_id,
    )
    upload_to_vm(
        client,
        Path(tool_use_capture.__file__).read_text(encoding="utf-8"),
        f"{rec_dir}/tool_use_capture.py",
        instance_id=instance_id,
    )
    upload_to_vm(
        client,
        Path(agent_invocation.__file__)
        .with_name("desktop_capture.py")
        .read_text(encoding="utf-8"),
        f"{rec_dir}/desktop_capture.py",
        instance_id=instance_id,
    )
    upload_to_vm(
        client,
        Path(trajectory.__file__).read_text(encoding="utf-8"),
        f"{rec_dir}/trajectory.py",
        instance_id=instance_id,
    )

    # The recreation script invokes the shared probe only after it has built/launched the
    # reference.  Ship the self-contained modules now; executing the standalone setup here would
    # lock the frozen inputs before they are consumed.
    permission_probe.ship_modules(client, "/tmp/rb_permission")

    upload_to_vm(
        client, script, f"{rec_dir}/stage_recreation.sh", instance_id=instance_id
    )
    return run_command(
        client,
        f"{env_prefix}bash {rec_dir}/stage_recreation.sh",
        instance_id=instance_id,
        timeout=timeout,
    )
