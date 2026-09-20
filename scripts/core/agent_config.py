"""One claude-code / codex configuration for all five platforms.

`mcp_settings.py` unified how the agent is wired to its GUI driver. This module unifies the
config the agent itself is started with, which had five delivery mechanisms and five different
permission sets:

    linux    dict -> base64 -> `base64 -d` on the VM     $HOME/.claude/settings.json
    windows  dict -> json.dumps                          <workspace>/.claude/settings.json
    macOS    heredoc literal inside the .sh              $AGENT_RECREATION + $AGENT_HOME/.claude/
    web      dict rendered at runtime                    <workspace>/.claude/settings.local.json
    android  core CLI output rendered after rb fetch     $HOME/.claude/settings.json

The mechanisms are genuinely constrained by where each platform runs, so what is shared here is
the CONTENT plus one renderer; the delivery stays per-platform (python platforms import this,
shell platforms call the CLI, exactly as they already do for core.trajectory).

LINUX IS THE REFERENCE, per the operator's instruction, and it is also the strictest: it was the
only platform denying the full interactive/subagent set. That matters because `deny` is the half
that actually takes effect -- verified on claude 2.1.237, an MCP tool runs even with no allow
entry at all, while a deny removes the tool from the model's list entirely. So the divergence
was not cosmetic: some workers exposed WebFetch/WebSearch and subagents during a benchmark whose
premise is offline, single-agent recreation from observation.

`Task(*)` is denied alongside `Agent(*)` because 2.1.177 keys permissions on `Agent` but surfaces
the tool as `Task` in the init snapshot; only linux denied both spellings.
"""

from __future__ import annotations

import base64
import json

from core import agent_invocation
from core.model_endpoint import ModelEndpoint

# ── permission vocabulary ────────────────────────────────────────────────────────────────
# Everything a recreation legitimately needs: author files and run builds.
BASE_ALLOW: tuple[str, ...] = (
    "Bash(*)",
    "Read(*)",
    "Write(*)",
    "Edit(*)",
    "Glob(*)",
    "Grep(*)",
)

# Both spellings of the subagent tool. Release recreation denies both.
SUBAGENT_ALLOW: tuple[str, ...] = ("Agent(*)",)
SUBAGENT_DENY: tuple[str, ...] = ("Agent(*)", "Task(*)")

# The benchmark is offline by construction: the reference is served locally and reaching the real
# internet would let the agent look up the original instead of observing it.
OFFLINE_DENY: tuple[str, ...] = ("WebFetch(*)", "WebSearch(*)")

# `-p` is non-interactive, so a tool that waits for a human can only stall the run. Workflow is
# included because macOS already denied it and a recreation has no business fanning out.
NONINTERACTIVE_DENY: tuple[str, ...] = (
    "EnterPlanMode(*)",
    "ExitPlanMode(*)",
    "AskUserQuestion(*)",
    "Monitor(*)",
    "PushNotification(*)",
    "Workflow(*)",
)


def claude_settings(
    *,
    mcp_servers: tuple[str, ...] | list[str] = (),
    subagents: bool = False,
    extra_allow: tuple[str, ...] | list[str] = (),
    extra_deny: tuple[str, ...] | list[str] = (),
    env: dict[str, str] | None = None,
    scope: str | None = None,
) -> dict:
    """The settings.json body.

    ``scope``: the interaction scope this task grants (core.scope). Under ``desktop_session`` -- the
    confirmed RecreationBench Desktop default -- the agent keeps whole-session observation and may
    select desktop capture. Under ``application`` scope, tools that widen observation past one app
    are denied, including the driver's own ``set_config``. Omitted means no scope-derived deny,
    i.e. today's behaviour unchanged.

    ``subagents``: release recreation is single-agent. The option remains for non-release
    callers that intentionally fan out. Anything denied is NEVER also allowed --
    deny wins in claude-code, so listing a tool in both is at best confusing and at worst hides
    an intent, which is how android ended up allowing `Agent(*)` in settings.json while denying
    it on the command line.
    """
    from . import mcp_settings

    allow = list(BASE_ALLOW)
    if subagents:
        allow += list(SUBAGENT_ALLOW)
    for server in mcp_servers:
        allow += mcp_settings.permission_entries(server)
    allow += list(extra_allow)

    deny = list(OFFLINE_DENY) + list(NONINTERACTIVE_DENY) + list(extra_deny)
    if scope is not None:
        from . import scope as scope_mod

        deny += scope_mod.observation_deny(scope, tuple(mcp_servers))
        deny += scope_mod.config_mutation_deny(scope, tuple(mcp_servers))
    if not subagents:
        deny += list(SUBAGENT_DENY)

    denied = set(deny)
    allow = [a for a in dict.fromkeys(allow) if a not in denied]
    settings: dict = {
        "permissions": {"allow": allow, "deny": list(dict.fromkeys(deny))}
    }
    if env:
        settings["env"] = dict(env)
    return settings


def claude_settings_json(indent: int | None = None, **kw) -> str:
    return json.dumps(claude_settings(**kw), indent=indent, ensure_ascii=False)


def claude_settings_b64(**kw) -> str:
    """For the platforms that ship settings through a shell heredoc."""
    return base64.b64encode(claude_settings_json(**kw).encode()).decode()


def allowed_tools_args(**kw) -> list[str]:
    """The allow list as argv elements for `--allowedTools`.

    Kept in the same module as the settings so the CLI flag and the file cannot drift; they
    had, on every platform that passed both.
    """
    return list(claude_settings(**kw)["permissions"]["allow"])


def disallowed_tools_args(**kw) -> list[str]:
    return list(claude_settings(**kw)["permissions"]["deny"])


# ── codex ────────────────────────────────────────────────────────────────────────────────
def codex_config_toml(
    model: str,
    endpoint: ModelEndpoint,
) -> str:
    """codex's config.toml. Top-level keys FIRST, provider table last.

    The order is load-bearing: codex reads `model_reasoning_effort` and friends as TOP-LEVEL
    keys, so anything appended after the `[model_providers.*]` table gets nested under it and
    silently ignored (VM-confirmed on codex 0.142.3/0.145.0). Callers that add effort lines must
    insert before the table -- `insert_top_level` does that for them.
    """
    lines = [
        f'model = "{model}"',
        f'model_provider = "{endpoint.provider}"',
        "",
        f"[model_providers.{endpoint.provider}]",
        f'name = "{endpoint.name}"',
        f'base_url = "{endpoint.base_url}"',
        f'env_key = "{endpoint.credential_env}"',
    ]
    if endpoint.wire_api:
        lines.append(f'wire_api = "{endpoint.wire_api}"')
    if endpoint.supports_websockets is not None:
        value = "true" if endpoint.supports_websockets else "false"
        lines.append(f"supports_websockets = {value}")
    # Without these codex exhausts its own default budget before the sidecar's backoff can
    # help, ending the rollout on "exceeded retry limit, last status: 429".
    lines.append(f"request_max_retries = {endpoint.request_max_retries}")
    lines.append(f"stream_max_retries = {endpoint.stream_max_retries}")
    return "\n".join(lines) + "\n"


def insert_top_level(config: str, lines: str) -> str:
    """Insert top-level TOML keys BEFORE the first [model_providers.*] table."""
    i = config.find("[model_providers")
    return config[:i] + lines + config[i:] if i != -1 else config + lines


def mcp_server_toml(
    server: str,
    command: str,
    args: list[str] | tuple[str, ...],
    *,
    env: dict[str, str] | None = None,
    env_vars: list[str] | tuple[str, ...] | None = None,
    startup_timeout_sec: int | None = None,
    tool_timeout_sec: int | None = None,
    required: bool | None = None,
) -> str:
    """A codex `[mcp_servers.<name>]` block."""
    lines = [
        "",
        f"[mcp_servers.{server}]",
        f"command = {json.dumps(command)}",
        f"args = {json.dumps(list(args))}",
    ]
    if startup_timeout_sec is not None:
        lines.append(f"startup_timeout_sec = {int(startup_timeout_sec)}")
    if tool_timeout_sec is not None:
        lines.append(f"tool_timeout_sec = {int(tool_timeout_sec)}")
    if required is not None:
        lines.append(f"required = {'true' if required else 'false'}")
    if env_vars:
        lines.append(f"env_vars = {json.dumps(list(env_vars))}")
    if env:
        lines.append(f"[mcp_servers.{server}.env]")
        lines.extend(f"{key} = {json.dumps(str(value))}" for key, value in env.items())
    return "\n".join(lines) + "\n"


# The `[1m]` label is CLAUDE-CODE-ONLY. Claude Code reads its context window off the model
# NAME, so the suffix is how a 1M window gets requested; the anthropic-side proxy strips it
# before the upstream ever sees it. codex has no such convention and does not use the label at
# all. Defined once here so the two agents' model params cannot be conflated again.
CLAUDE_ONLY_MODEL_SUFFIX = agent_invocation.CLAUDE_CONTEXT_SUFFIX


def claude_code_model(model: str = "", *, context_1m: bool) -> str:
    """Claude Code alias, context-decorated when a 1M window is wanted.

    An empty value selects the fixed ``claude-opus-4-8`` alias. Idempotence lets
    VM-local shell boundaries safely apply the same context decoration again.

    NEVER feed the result to codex: see codex_model_slug.
    """
    return agent_invocation.claude_client_model(model, context_1m=context_1m)


def codex_model_slug(model: str) -> str:
    """Bare model slug for codex's own config (metadata lookup).

    codex-cli 0.145.0's registry finds metadata for the bare slug (gpt-5.6-sol) but NOT for a
    provider-prefixed id (openai.gpt-5.6-sol / gpt-5.6) -- the prefixed form logs "Model
    metadata not found. Defaulting to fallback metadata" and degrades the context window /
    token limits (VM-verified 2026-07-23). Keep the model bare at both the Codex and
    upstream gateway boundaries.

    A trailing '[1m]' is also stripped. That token is a CLAUDE-CODE-ONLY convention: Claude
    Code parses the context window off the model NAME, and the anthropic-side proxy strips the
    suffix before it reaches the upstream. Codex has no such convention and (on a direct
    Responses route) sends a '[1m]' name verbatim, which some gateways reject with an
    auth-shaped error for an unknown model id. Android exports both RECREATION_MODEL (upstream,
    clean) and RECREATION_CLAUDE_MODEL (decorated), and setup_codex read the decorated one.

    General rule: strip a single leading '<provider>.' prefix and a single trailing '[1m]'.
    """
    return agent_invocation.normalize_model("codex", model)


def _main(argv: list[str]) -> int:
    """Render agent configuration for shell platforms without inline Python."""
    import argparse
    from pathlib import Path

    ap = argparse.ArgumentParser(prog="core.agent_config")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("settings", help="print settings.json")
    s.add_argument("--mcp-server", action="append", default=[])
    s.add_argument("--subagents", action="store_true")
    s.add_argument("--allow", action="append", default=[])
    s.add_argument("--deny", action="append", default=[])
    s.add_argument("--scope", default=None)
    s.add_argument("--indent", type=int, default=2)
    c = sub.add_parser("codex", help="print a complete Codex config.toml")
    c.add_argument("--model", required=True)
    c.add_argument("--base-url", required=True)
    c.add_argument("--provider", required=True)
    c.add_argument("--name", required=True)
    c.add_argument("--wire-api", default="")
    c.add_argument(
        "--supports-websockets",
        choices=("auto", "true", "false"),
        default="auto",
    )
    c.add_argument("--credential-env", default="OPENAI_API_KEY")
    c.add_argument("--request-max-retries", type=int, default=10)
    c.add_argument("--stream-max-retries", type=int, default=10)
    c.add_argument("--reasoning-effort", default="")
    c.add_argument("--mcp-config")
    c.add_argument("--mcp-server")
    c.add_argument("--mcp-startup-timeout-sec", type=int)
    c.add_argument("--mcp-tool-timeout-sec", type=int)
    a = ap.parse_args(argv)
    if a.cmd == "settings":
        print(
            claude_settings_json(
                indent=a.indent,
                mcp_servers=tuple(a.mcp_server),
                subagents=a.subagents,
                extra_allow=tuple(a.allow),
                extra_deny=tuple(a.deny),
                scope=a.scope,
            )
        )
    elif a.cmd == "codex":
        websocket_value = {
            "auto": None,
            "true": True,
            "false": False,
        }[a.supports_websockets]
        endpoint = ModelEndpoint(
            provider=a.provider,
            name=a.name,
            base_url=a.base_url,
            wire_api=a.wire_api,
            credential_env=a.credential_env,
            supports_websockets=websocket_value,
            request_max_retries=a.request_max_retries,
            stream_max_retries=a.stream_max_retries,
        )
        rendered = codex_config_toml(codex_model_slug(a.model), endpoint)
        effort = str(a.reasoning_effort or "").strip().lower()
        if effort:
            rendered = insert_top_level(
                rendered, f'model_reasoning_effort = "{effort}"\n'
            )
        if bool(a.mcp_config) != bool(a.mcp_server):
            ap.error("--mcp-config and --mcp-server must be provided together")
        if a.mcp_config:
            from . import mcp_settings

            spec = json.loads(Path(a.mcp_config).read_text())["mcpServers"]
            if set(spec) != {a.mcp_server}:
                raise ValueError(
                    f"Codex requires exactly {a.mcp_server!r}, got {sorted(spec)}"
                )
            server = spec[a.mcp_server]
            tool_timeout_sec = a.mcp_tool_timeout_sec
            if tool_timeout_sec is None:
                tool_timeout_sec = (
                    int(
                        mcp_settings.tool_timeout_ms(
                            __import__("os").environ.get(
                                mcp_settings.TOOL_TIMEOUT_OVERRIDE_VAR
                            )
                        )
                    )
                    // 1000
                )
            rendered += mcp_server_toml(
                a.mcp_server,
                server["command"],
                server.get("args", []),
                env=server.get("env") or None,
                startup_timeout_sec=a.mcp_startup_timeout_sec,
                tool_timeout_sec=tool_timeout_sec,
            )
        print(rendered, end="")
    return 0


if __name__ == "__main__":  # pragma: no cover
    import sys

    raise SystemExit(_main(sys.argv[1:]))
