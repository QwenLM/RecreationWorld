"""One MCP wiring contract for all five platforms.

``core.cua_driver`` selects the platform driver. This module defines the matching
client configuration: server names, exact package versions, per-call timeouts, and
permission spellings.

Every MCP call has a finite timeout so a stalled driver cannot consume the remaining
job budget. Permission entries use the documented bare-server and per-tool wildcard
forms. The deny entries remain authoritative for hiding tools from the model.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess

from . import cua_driver

# ── server names: what the AGENT sees ────────────────────────────────────────────────────
# One binary must have one name. cua-driver is driven by Linux, macOS, and Windows;
# it used to answer to `desktop-control`, `cua-computer-use` and `cua-driver` depending on the
# platform and CLI, which made prompt text non-portable and left windows+codex naming a server
# that was not the one registered.
DESKTOP_SERVER = "desktop-control"  # cua-driver on the three desktop platforms
MOBILE_SERVER = "mobile-mcp"  # android emulator over adb
BROWSER_SERVER = "playwright"  # the only Web browser MCP

SERVER_NAMES = (DESKTOP_SERVER, MOBILE_SERVER, BROWSER_SERVER)

# ── released server packages ─────────────────────────────────────────────────────────────
# An MCP server is part of the benchmark environment, just like the agent CLI and cua-driver.
# Floating npm resolution changes the available tools and their schemas between runs, while a
# bare package name can silently select whichever global/cache copy happens to be in the image.
PLAYWRIGHT_PACKAGE = "@playwright/mcp@0.0.79"
PLAYWRIGHT_BINARY = "playwright-mcp"
MOBILE_PACKAGE = "@qwen-code/mobile-mcp@0.1.5"


def _exact_npm_package(spec: str) -> tuple[str, str]:
    """Split an exact npm package spec, including scoped package names."""
    package, sep, version = spec.rpartition("@")
    if not sep or not package or not version or version in {"latest", "next"}:
        raise ValueError(f"npm package must carry an exact version: {spec!r}")
    return package, version


def global_npm_version(package: str) -> str:
    """Return the globally installed package version, or an empty string."""
    proc = subprocess.run(
        ["npm", "list", "-g", "--depth=0", "--json", package],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    try:
        payload = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return ""
    return str(payload.get("dependencies", {}).get(package, {}).get("version", ""))


def ensure_global_npm_package(spec: str, binary: str) -> str:
    """Converge a rolling image to one exact MCP package and verify its executable."""
    package, expected = _exact_npm_package(spec)
    installed = global_npm_version(package)
    if installed != expected:
        print(
            f"MCP package {package} {installed or 'missing'} != pin {expected}; installing"
        )
        subprocess.run(
            ["npm", "install", "-g", "--no-audit", "--no-fund", spec],
            check=True,
        )
        installed = global_npm_version(package)
    if installed != expected:
        raise RuntimeError(
            f"MCP package {package} version {installed or 'missing'}; expected {expected}"
        )
    executable = shutil.which(binary)
    if not executable:
        raise RuntimeError(
            f"MCP executable is missing after installing {spec}: {binary}"
        )
    print(f"MCP package ready: {package}@{installed} ({executable})")
    return executable


# ── per-call wall clock ──────────────────────────────────────────────────────────────────
TOOL_TIMEOUT_VAR = "MCP_TOOL_TIMEOUT"
TOOL_TIMEOUT_OVERRIDE_VAR = "RB_MCP_TOOL_TIMEOUT"

# 180 s. Chosen against the two numbers that bracket it: the median inter-event gap in the
# affected trajectories is ~29 s, and the hangs ran 260-307 min. Anything in between catches the
# hang without truncating real work, so this is not a delicate constant. It also survives both
# resolver generations unchanged (above the 60 s floor of the older one, far below 2^31-1), and
# 8000 was honoured verbatim on 2.1.237, so the floor here is lower still.
DEFAULT_TOOL_TIMEOUT_MS = "180000"

# Server STARTUP timeout, a different knob. Left unset deliberately: no platform has ever
# reported a slow-starting MCP server, and a wrong value here fails the server closed, which
# costs an entire run. Named so the next reader does not mistake TOOL_TIMEOUT for it.
STARTUP_TIMEOUT_VAR = "MCP_TIMEOUT"


def tool_timeout_ms(override: str | None = None) -> str:
    """The per-call bound, as a string of milliseconds.

    ``override`` is the value of ``RB_MCP_TOOL_TIMEOUT`` when the caller has it in hand; an
    empty or blank override means "not set" and yields the default, so callers can pass
    ``os.environ.get(...)`` straight through.
    """
    value = (override or "").strip()
    return value or DEFAULT_TOOL_TIMEOUT_MS


def tool_timeout_env(override: str | None = None) -> dict[str, str]:
    """For an mcpServers ``env`` block or a subprocess env dict."""
    return {TOOL_TIMEOUT_VAR: tool_timeout_ms(override)}


def tool_timeout_export_sh() -> str:
    """A POSIX-shell line for the heredocs linux and macOS send to a VM.

    Emits the indirection rather than a baked number so an operator can retune a live run
    without a redeploy.
    """
    return (
        f"export {TOOL_TIMEOUT_VAR}="
        f"${{{TOOL_TIMEOUT_OVERRIDE_VAR}:-{DEFAULT_TOOL_TIMEOUT_MS}}}"
    )


def tool_timeout_export_ps1(override: str | None = None) -> str:
    """The PowerShell equivalent for the windows VM scripts."""
    return f"$env:{TOOL_TIMEOUT_VAR} = '{tool_timeout_ms(override)}'"


# ── permission entries ───────────────────────────────────────────────────────────────────
def permission_entries(server: str) -> list[str]:
    """The canonical allow/deny spellings for one MCP server.

    Both documented forms: bare (whole server) and the per-tool wildcard. Emitting both is a
    superset of every spelling the five platforms used, and `deny` — the half that actually
    takes effect — honours both.
    """
    if not server:
        raise ValueError("server name is required")
    return [f"mcp__{server}", f"mcp__{server}__*"]


def allowed_tools_args(server: str) -> list[str]:
    """Entries for the ``--allowedTools`` CLI flag (one argv element each)."""
    return permission_entries(server)


def tool_prefix(server: str) -> str:
    """``mcp__<server>__`` — the prefix prompts use to name individual tools."""
    return f"mcp__{server}__"


# ── the mcpServers entry ─────────────────────────────────────────────────────────────────
def server_entry(
    command: str,
    args: list[str] | tuple[str, ...] | None = None,
    env: dict[str, str] | None = None,
    *,
    stdio: bool = True,
) -> dict:
    """One mcpServers entry.

    ``type: "stdio"`` is written explicitly. It is the default, so omitting it worked, but only
    macOS said so and a reader could not tell whether the other four had chosen something else.
    """
    entry: dict = {}
    if stdio:
        entry["type"] = "stdio"
    entry["command"] = command
    entry["args"] = list(args or [])
    if env:
        entry["env"] = dict(env)
    return entry


def desktop_env(
    *,
    normalize: bool = False,
    scale: str = cua_driver.DEFAULT_SCALE,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    """The env for a cua-driver MCP entry: the shared daemon contract plus platform extras.

    ``cua_driver.driver_env()`` has declared this contract since the driver unification and no
    platform called it — macOS seeded 3 of its 4 vars, windows and web 2, linux 0 unless
    ``RB_CUA_COORDINATE_SPACE=1`` installed a wrapper. ``UPDATE_CHECK=0`` is the one that
    matters beyond tidiness: a driver that upgrades itself mid-benchmark is exactly the
    confound the pin exists to prevent.

    ``extra`` carries the genuinely per-platform keys (macOS' ``MCP_FORCE_PROXY``, windows'
    ``MCP_MODEL_PAYLOAD_FILTER``, web's ``CDP_PORT``/``DISPLAY``) and wins on conflict.
    """
    env = cua_driver.driver_env(normalize=normalize, scale=scale)
    if extra:
        env.update({k: v for k, v in extra.items() if v is not None})
    return env


def mcp_config(
    server: str,
    command: str,
    args: list[str] | tuple[str, ...] | None = None,
    env: dict[str, str] | None = None,
    *,
    stdio: bool = True,
) -> dict:
    """A complete ``--mcp-config`` / ``.claude/mcp.json`` document for one server."""
    return {"mcpServers": {server: server_entry(command, args, env, stdio=stdio)}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Converge pinned MCP runtime packages")
    parser.add_argument("action", choices=("ensure-playwright",))
    args = parser.parse_args(argv)
    if args.action == "ensure-playwright":
        ensure_global_npm_package(PLAYWRIGHT_PACKAGE, PLAYWRIGHT_BINARY)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
