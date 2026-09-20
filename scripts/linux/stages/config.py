"""Shared configuration for all pipeline stages."""

import os
from pathlib import Path

# VM base path for all pipeline data
VM_PIPELINE_BASE = "/home/user/rb_pipeline"

# Complete controller code synced into the guest.  It stays root-owned and mode
# 0700: stage preparation can execute shared helpers from it, but the recreation
# agent (``user``) must never be able to inspect benchmark implementation details.
VM_CODE_DIR = "/opt/recreationbench"

# Docker image
DEFAULT_IMAGE = "rb-gui-builder:latest"

# Model routing is supplied by the deployment boundary.
DEFAULT_MODEL = ""

# Agents directory
AGENTS_DIR = Path(__file__).parent.parent.parent.parent / ".claude" / "agents"

# Claude Code settings: allow all tools, deny web access
BUILD_SETTINGS_B64 = (
    "eyJwZXJtaXNzaW9ucyI6eyJhbGxvdyI6WyJCYXNoKCopIiwiUmVhZCgqKSIsIldyaXRl"
    "KCopIiwiRWRpdCgqKSIsIkdsb2IoKikiLCJHcmVwKCopIiwiQWdlbnQoKikiXSwiZGVueSI6W119fQ=="
)

# Install Claude Code command (reusable across stages)
# NOTE: dollar signs escaped for embedding inside bash -c "..."
CLAUDE_CODE_VERSION = "2.1.177"
INSTALL_CLAUDE = (
    f"curl -fsSL --retry 5 --retry-all-errors --retry-delay 5 "
    f'"https://downloads.claude.ai/claude-code-releases/{CLAUDE_CODE_VERSION}/linux-x64/claude" '
    "-o /usr/local/bin/.claude.rb-download && "
    "chmod +x /usr/local/bin/.claude.rb-download && "
    "mv -f /usr/local/bin/.claude.rb-download /usr/local/bin/claude"
)

# Codex CLI (OpenAI) — for GPT-series model evaluation.
# NOTE: the REAL (pinned) install is in vm_bootstrap.py's bootstrap script; this
# constant is informational only. 0.145.0 is deployment platform-validated with the codex-mcp fixes
# (stdin prompt / --dangerously-bypass-approvals-and-sandbox / root streaming
# proxy). The earlier 0.145.0 "sandbox-fragile" worry was superseded once those
# fixes landed (round-4 canary ran the recreation ~23min on 0.145.0).
CODEX_CLI_VERSION = "0.145.0"

# codex writes {model} verbatim into ~/.codex/config.toml. codex-cli 0.145.0 only
# finds model metadata for the BARE slug (e.g. gpt-5.6-sol); a provider-prefixed id
# (openai.gpt-5.6-sol) → "metadata not found → fallback" (wrong ctx window). So run()
# feeds codex_model_slug(model) here. ``base_url`` is a placeholder that the
# agent-side script replaces with its loopback relay. The deployment adapter owns
# the relay's upstream endpoint and any provider-specific model-name translation.


def _codex_template(platform: str) -> str:
    """The platform's config.toml as a {model}/{base_url} format string."""
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
    from core import agent_config as _ac
    from core.model_endpoint import from_environment as _endpoint_from_environment

    if platform != "linux":
        raise ValueError(f"unsupported Linux config platform: {platform!r}")
    endpoint = _endpoint_from_environment(
        os.environ,
        provider="recreationbench",
        name="RecreationBench model endpoint",
        base_url="{base_url}",
    )
    return _ac.codex_config_toml("{model}", endpoint)


# Built by the shared endpoint and config renderers, which reproduce
# this template byte-for-byte. `.format(model=…, base_url=…)` still works, so no caller
# changes. The provider name and wire_api stay per-platform on purpose: Linux keeps its
# historical implicit wire selection while Windows sets wire_api="responses", so folding
# them into one value would be a silent protocol change.
CODEX_CONFIG_TOML_TEMPLATE = _codex_template("linux")

# Build prompt
BUILD_PROMPT = """\
You are a build engineer on an Ubuntu 22.04 VM with pre-installed build
tools (cmake, meson, qmake, Qt5/Qt6, GTK3, wxWidgets, Node.js 20, SDL2,
FFmpeg libs, Xvfb). You are running as root.

A display is already running (check $DISPLAY). Do NOT start, stop, or
kill Xvfb or any display server; it is managed by the pipeline. Use the
existing DISPLAY as-is.

The application source is already checked out at /workspace/repo at a pinned commit,
with submodules materialised. build.sh MUST NOT acquire source: no git clone, fetch,
checkout, reset, clean or submodule commands against /workspace/repo, and never delete
it. Fetching a third-party dependency into a different directory is fine.

Your task:
1. Read README.md, CONTRIBUTING.md, CMakeLists.txt, Makefile, meson.build,
   .github/workflows/, or any build docs to understand how to compile.
2. Install any missing dependencies with `apt-get install -y`.
3. Build the project (prefer Release mode, install to /workspace/install).
4. Write a standalone build script to /workspace/output/build.sh that
   reproduces the entire build from scratch (including apt-get commands).
   The script must be self-contained and run as root without sudo.
5. Write a launch script to /workspace/output/launch.sh that starts the
   application. The launch script must:
   - Set all runtime environment variables (LD_LIBRARY_PATH, XDG_DATA_DIRS,
     GSETTINGS_SCHEMA_DIR, GI_TYPELIB_PATH, QT_PLUGIN_PATH, etc.) as needed
   - Run glib-compile-schemas if the app uses GSettings
   - Handle any app-specific flags (e.g. --no-sandbox for Electron)
   - REQUIRED for genuine Electron/Chromium apps ONLY: export
     ELECTRON_ENABLE_ACCESSIBILITY=1 AND pass --force-renderer-accessibility on
     the app's own command line (alongside --no-sandbox). Without
     --force-renderer-accessibility Chromium exposes NO AT-SPI tree (only a bare
     top-level window), which breaks downstream accessibility testing. This
     applies to Electron/Chromium ONLY — Tauri/WebKitGTK and native GTK/Qt apps
     do NOT use this flag (they expose AT-SPI via the GTK/ATK bridge with
     NO_AT_BRIDGE=0); do not add it to their launch scripts.
   - AT-SPI requires the app on the SHARED session bus: do NOT wrap it in a
     private `dbus-run-session` when DBUS_SESSION_BUS_ADDRESS is already set —
     that isolates the AT-SPI tree onto a private bus the test harness cannot
     see (it will observe only a bare 1-element window). Join the existing bus.
   - Electron/Chromium apps: use a UNIQUE per-launch --user-data-dir (e.g. a
     path under `mktemp -d`), NOT a fixed /tmp path. The test harness relaunches
     the app once per test module; a shared user-data-dir triggers single-instance
     lock collisions so later modules fail to launch (baseline 0/N).
   - exec the binary, passing through "$@"
   - Work standalone without build.sh having been sourced first
6. Verify from clean state: remove /workspace/install, run build.sh against the
   existing /workspace/repo, then verify the app launches via launch.sh (it must
   start its GUI without crashing). Any source patches, sed commands, or dependencies needed must
   be included in build.sh; any runtime env setup must be in launch.sh.
7. Write a short JSON summary to /workspace/output/build_result.json:
   {"success": true/false, "executable": "<path>", "build_system": "<cmake/meson/...>",
    "installed_deps": ["pkg1", ...], "notes": "..."}

If cmake/meson/configure fails, read the error, install missing deps, and retry.
You have full root access. Use `apt-cache search` to find packages.
Do not use sudo in /workspace/output/build.sh; the script is run as root in
later pipeline stages and eval containers may not have sudo installed.
Do not give up after the first failure.

IMPORTANT: These are GUI desktop applications. The verification in step 6 must
actually launch the GUI (not just --help/--version). Use `timeout 10 bash
/workspace/output/launch.sh` and confirm the process stays alive for a few
seconds without crashing. If it crashes (GSettings errors, missing libs, etc.),
fix launch.sh or build.sh and retry.

Do NOT sanity-check the built binary with `<app> --version` or `<app> --help`:
GUI toolkits (Tauri, Electron, some GTK apps) launch the full GUI on any argument
and never exit, which HANGS the build and burns the entire time budget. Verify with
`test -x <executable>` plus `timeout 10 bash /workspace/output/launch.sh`. Wrap
EVERY direct invocation of the app in `timeout`; if a command hangs, kill it at once
(pkill) and move on -- never spend turns recovering from a hung GUI process. As soon
as the binary exists and stays alive under `timeout`, write build_result.json and
STOP -- do not keep rebuilding or re-verifying.\
"""


def vm_task_dir(task_id: str) -> str:
    # Fixed name: each VM runs one task; avoids leaking app name to recreation agent
    return f"{VM_PIPELINE_BASE}/task"


def codex_model_slug(model: str) -> str:
    """Bare model slug for codex's own config -- see core.agent_config.codex_model_slug.

    Kept as a thin re-export because the alias/upstream split is now ONE definition: the
    same rule has to hold for codex's config.toml (here) and for the litellm alias the
    deployment integration sends. Two copies of it would drift into a
    "metadata not found" fallback that only shows up as a degraded context window.
    """
    import sys as _sys
    from pathlib import Path as _Path

    _sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))
    from core import agent_config as _ac

    return _ac.codex_model_slug(model)
