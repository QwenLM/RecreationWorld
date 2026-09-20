"""Single-task runner for RecreationBench Web.

Orchestrates the full agent workflow for one task:
1. Serve the original site
2. Prepare agent workspace (template files at root + task.json)
3. Run the agent (claude-code CLI with browser MCP)
4. Build agent output (npm run build)
5. Evaluate the result
"""

import argparse
import asyncio
import contextlib
import json
import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from config import (
    BROWSER_MCP_CONFIGS,
    DEFAULT_BROWSER_MCP,
    TEMPLATE_DIR,
    image_read_allowed,
    vlm_judge_max_concurrency,
)
from core import (
    agent_config,
    agent_invocation,
    exit_contract,
    mcp_probe,
    recreation_paths,
    recreation_prompt,
)
from core import mcp_settings as _mcp
from core.pipeline import normalize_cua_preflight_mode
from serving.site_server import SiteServer

logger = logging.getLogger(__name__)

DEFAULT_AGENT_TIMEOUT_SEC = agent_invocation.DEFAULT_AGENT_TIMEOUT_SEC
DEFAULT_TASK_TIMEOUT_SEC = 7200
DEFAULT_TIMEOUT_MULTIPLIER = 10.0
DEFAULT_API_TIMEOUT_MS = str(agent_invocation.DEFAULT_REQUEST_TIMEOUT_MS)

def prepare_workspace(
    workspace_dir: Path,
    task_json: dict,
    site_url: str,
    allow_image_read: bool = True,
    browser_mcp: str = DEFAULT_BROWSER_MCP,
) -> Path:
    """Prepare the agent workspace with template at root and task definition.

    Creates:
        workspace/
        ├── package.json       (from template)
        ├── vite.config.ts     (from template)
        ├── tsconfig.json      (from template)
        ├── index.html         (from template)
        ├── src/               (from template)
        ├── public/            (from template)
        ├── task.json          (task definition with filled site_url)
        └── output/            (empty, for agent's build output)
    """
    workspace = Path(workspace_dir)

    if workspace.exists():
        shutil.rmtree(workspace)
    shutil.copytree(
        str(TEMPLATE_DIR),
        str(workspace),
        ignore=shutil.ignore_patterns("node_modules", "dist", ".git"),
    )

    # deployment adapter's image already contains the template's exact node_modules tree.
    # Link that immutable, root-owned runtime into each throw-away workspace rather
    # than copying hundreds of MB or asking npm to reach the public registry from a
    # network-sealed build.  main.sh verifies the image and rb package-lock files
    # match before setting this variable, so a stale image fails before rollout.
    seeded_node_modules = os.environ.get("MOCKWEB_TEMPLATE_NODE_MODULES", "").strip()
    if seeded_node_modules:
        seed = Path(seeded_node_modules)
        if not seed.is_dir():
            raise RuntimeError(
                "MOCKWEB_TEMPLATE_NODE_MODULES does not name a directory: "
                f"{seeded_node_modules}"
            )
        (workspace / "node_modules").symlink_to(seed, target_is_directory=True)

    for subdir in [
        "src/pages",
        "src/components",
        "src/assets",
        "src/styles",
        "public/images",
    ]:
        (workspace / subdir).mkdir(parents=True, exist_ok=True)

    (workspace / "output").mkdir(parents=True, exist_ok=True)

    task_with_url = dict(task_json)
    if isinstance(task_with_url.get("constraints"), dict):
        task_with_url["constraints"] = dict(task_with_url["constraints"])
        # Page ids remain private evaluator input. The recreation agent discovers the full site.
        task_with_url["constraints"].pop("target_pages", None)
    if "input" in task_with_url:
        task_with_url["input"] = dict(task_with_url["input"])
        task_with_url["input"]["site_url"] = site_url
        # The executable task instruction comes only from recreation_prompt.render(). Keep
        # task.json as runtime metadata rather than exposing a second, potentially drifting prompt.
        task_with_url["input"].pop("instructions", None)

    with open(workspace / "task.json", "w", encoding="utf-8") as f:
        json.dump(task_with_url, f, indent=2, ensure_ascii=False)

    # Bound every MCP tool call before the agent is spawned: _spawn inherits this process's env
    # (setpriv keeps the parent env). Unset means UNBOUNDED on this claude generation (verified
    # on 2.1.237: a 600 s stub-server hang was never cut off), so one hung browser-MCP call
    # would silently consume the whole time limit.
    #
    # A deployment adapter normally exports this before entering RB. This block is
    # the fallback for direct invocation (local dev, batch_runner), and it must not
    # warn when the environment is already set.
    if not os.environ.get(_mcp.TOOL_TIMEOUT_VAR, "").strip():
        os.environ[_mcp.TOOL_TIMEOUT_VAR] = _mcp.tool_timeout_ms(
            os.environ.get(_mcp.TOOL_TIMEOUT_OVERRIDE_VAR)
        )
    if not os.environ.get(_mcp.TOOL_TIMEOUT_VAR, "").strip():
        raise RuntimeError(
            "MCP tool timeout is empty; refusing an unbounded recreation"
        )

    _write_workspace_claude_settings(
        workspace,
        browser_mcp=browser_mcp,
        allow_image_read=allow_image_read,
    )

    return workspace


def _apply_image_ban(settings: dict) -> None:
    """Merge the image_guard PreToolUse/PostToolUse hooks into a settings dict.

    Idempotent: skips a hook if a matcher with the same key already exists, so it
    is safe to call on a settings file that may already carry the hooks. Used for
    the text-only evaluation mode (allow_image_read=False) — see
    runner/image_guard.py.
    """
    # image_guard.py lives next to this module (runner/) and is referenced by its
    # resolved path, so the Read/screenshot hooks work wherever runner/ sits. NOTE:
    # the hook is executed by claude (the de-privileged agent uid), so image_guard.py
    # must stay agent-readable — runner/ is NOT part of the root-0700 scorer isolation
    # (only evaluation/ is masked).
    hook_cmd = f"python3 {Path(__file__).resolve().parent / 'image_guard.py'}"
    hooks = settings.setdefault("hooks", {})
    pre = hooks.setdefault("PreToolUse", [])
    post = hooks.setdefault("PostToolUse", [])

    def _has(entries, matcher):
        return any(isinstance(e, dict) and e.get("matcher") == matcher for e in entries)

    if not _has(pre, "Read"):
        pre.append(
            {"matcher": "Read", "hooks": [{"type": "command", "command": hook_cmd}]}
        )
    if not _has(post, "mcp__.*browser_take_screenshot"):
        post.append(
            {
                "matcher": "mcp__.*browser_take_screenshot",
                "hooks": [{"type": "command", "command": hook_cmd}],
            }
        )


def _resolve_chromium_executable() -> str | None:
    """Find the Chromium binary installed by `playwright install chromium`.

    Returns an absolute path to the Chromium executable, or None if none is
    found. Honors PLAYWRIGHT_BROWSERS_PATH; otherwise looks under
    ~/.cache/ms-playwright. Picks the highest build number when several exist.
    Used to point @playwright/mcp at a browser that actually exists in the image
    (its default `chrome` channel / chrome-for-testing is not installed).
    """
    base = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    root = Path(base) if base else (Path.home() / ".cache" / "ms-playwright")
    if not root.is_dir():
        return None
    candidates: list[tuple[int, str]] = []
    for sub in root.glob("chromium-*"):
        m = re.search(r"chromium-(\d+)", sub.name)
        build = int(m.group(1)) if m else -1
        for rel in ("chrome-linux64/chrome", "chrome-linux/chrome"):
            exe = sub / rel
            if exe.is_file():
                candidates.append((build, str(exe)))
    if not candidates:
        return None
    return max(candidates, key=lambda t: t[0])[1]


def _prepare_browser_mcp_cfg(
    browser_mcp: str,
    allow_image_read: bool = True,
    *,
    cdp_endpoint: str = "",
) -> dict:
    """Build the browser-MCP server cfg shared by claude-code AND codex.

    For playwright, appends --executable-path pointing at the Chromium the image
    installed via `playwright install chromium`. Without it @playwright/mcp
    defaults to the `chrome` channel / chrome-for-testing, which is NOT
    installed → every browser_* call fails ("Chromium distribution 'chrome' is
    not found") and server startup can stall. --executable-path is
    version-agnostic (unlike --browser chromium, which maps to
    chrome-for-testing in recent @playwright/mcp). Unknown servers fail closed:
    running without an observation tool is not a valid recreation.
    """
    mcp_cfg = BROWSER_MCP_CONFIGS.get(browser_mcp)
    if not mcp_cfg:
        raise ValueError(f"Unknown browser MCP: {browser_mcp!r}")
    cfg = dict(mcp_cfg)
    args = list(cfg.get("args", []))
    chromium = _resolve_chromium_executable()
    if chromium:
        args += ["--executable-path", chromium]
    if cdp_endpoint:
        # The wrapper attaches to the capture browser when its CDP endpoint is
        # healthy and otherwise execs the original MCP args. Thus an optional
        # screenshot-sidecar failure cannot remove the agent's browser.
        original = str(cfg["command"])
        cfg["command"] = sys.executable
        args = [
            str(Path(__file__).resolve().with_name("playwright_mcp_capture.py")),
            original,
            cdp_endpoint,
            *args,
        ]
    elif chromium:
        logger.info("Playwright MCP will use bundled Chromium at %s", chromium)
    else:
        raise RuntimeError(
            "No bundled Chromium found under ms-playwright; refusing to start a "
            "Playwright MCP whose browser calls cannot succeed"
        )
    if not allow_image_read:
        args += ["--image-responses", "omit"]
    cfg["args"] = args
    return cfg


def setup_browser_mcp(
    browser_mcp: str,
    allow_image_read: bool = True,
    *,
    cdp_endpoint: str = "",
) -> str:
    """Write the browser-MCP config for claude-code.

    Returns the path to the written MCP config file (to be passed to the claude
    CLI via ``--mcp-config``). Unknown or unavailable servers fail closed.

    NOTE: claude-code does NOT auto-discover ``~/.claude/mcp.json``. MCP servers
    are only loaded from a project-root ``.mcp.json``, ``~/.claude.json``, or an
    explicit ``--mcp-config`` file. We therefore return this path so the caller
    can pass it explicitly; writing the file alone is not enough.

    When allow_image_read is False (text-only eval), append `--image-responses
    omit` to the Playwright MCP so browser_take_screenshot still runs/saves but
    returns no inline pixels. The workspace settings writer owns permissions and
    image-guard hooks; keeping those out of this function prevents user-home and
    workspace settings from drifting.
    """
    claude_dir = Path.home() / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)

    cfg = _prepare_browser_mcp_cfg(
        browser_mcp,
        allow_image_read=allow_image_read,
        cdp_endpoint=cdp_endpoint,
    )

    mcp_json = {"mcpServers": {_mcp.BROWSER_SERVER: cfg}}
    mcp_path = claude_dir / "mcp.json"
    mcp_path.write_text(json.dumps(mcp_json, indent=2), encoding="utf-8")
    # The Web orchestrator stays root while only the actual agent subprocess is
    # demoted.  HOME still points at /home/agent, so a fresh .claude created here
    # would otherwise be root:root 0755 and Claude could read mcp.json but could
    # not create its own session-env/projects state.  Android and macOS use the
    # same write-as-root-then-handoff contract; keep Web on that contract too.
    _chown_tree_to_agent(claude_dir)
    _attest_agent_claude_state_writable(claude_dir)
    logger.info(
        "Wrote MCP config for %s to %s (allow_image_read=%s)",
        browser_mcp,
        mcp_path,
        allow_image_read,
    )

    return str(mcp_path)


def _codex_config_path() -> Path:
    """Return the config path seen by the possibly de-privileged Codex child."""
    configured = os.environ.get("CODEX_HOME", "").strip()
    if configured:
        return Path(configured) / "config.toml"

    # The web orchestrator stays root while the rollout prefix changes the child HOME. Writing
    # root's ~/.codex would look successful but leave the actual Codex process without MCP.
    prefix = os.environ.get("MOCKWEB_AGENT_RUN_PREFIX", "").strip()
    if prefix:
        try:
            for token in shlex.split(prefix):
                if token.startswith("HOME=") and token[5:]:
                    return Path(token[5:]) / ".codex" / "config.toml"
        except ValueError as exc:
            raise RuntimeError(
                f"cannot resolve Codex HOME from MOCKWEB_AGENT_RUN_PREFIX: {exc}"
            ) from exc
    return Path.home() / ".codex" / "config.toml"


def setup_codex_browser_mcp(
    browser_mcp: str,
    allow_image_read: bool = True,
    *,
    cdp_endpoint: str = "",
) -> str:
    """Append the browser MCP as a codex [mcp_servers.*] table to ~/.codex/config.toml.

    codex learns MCP servers ONLY from its config.toml (no --mcp-config flag). main.sh
    Phase 2 already wrote the provider/auth block there; we APPEND (never overwrite) so
    codex auth is preserved. Idempotent. The destination is derived from CODEX_HOME or the
    de-privileging prefix's HOME, so it is the same file the Codex subprocess reads.

    Uses the same Playwright configuration source as claude-code, including the
    bundled Chromium executable path.
    """
    cfg = _prepare_browser_mcp_cfg(
        browser_mcp,
        allow_image_read=allow_image_read,
        cdp_endpoint=cdp_endpoint,
    )
    config_toml = _codex_config_path()
    if not config_toml.is_file():
        raise RuntimeError(
            f"Codex provider config is missing at {config_toml}; refusing to run without MCP"
        )
    server_name = _mcp.BROWSER_SERVER
    header = f"[mcp_servers.{server_name}]"
    existing = config_toml.read_text(encoding="utf-8")
    if header in existing:
        if not cdp_endpoint:
            return str(config_toml)
        # A fresh Web job normally has only the provider block, but replacing a
        # previously rendered Playwright table makes capture deterministic in
        # long-lived/local environments too. Include nested env tables in the
        # removed region and preserve every unrelated table verbatim.
        kept: list[str] = []
        skipping = False
        prefix = f"[mcp_servers.{server_name}."
        for line in existing.splitlines(keepends=True):
            stripped = line.strip()
            if stripped == header or stripped.startswith(prefix):
                skipping = True
                continue
            if skipping and stripped.startswith("["):
                skipping = False
            if not skipping:
                kept.append(line)
        existing = "".join(kept).rstrip() + "\n"

    fragment = agent_config.mcp_server_toml(
        server_name,
        cfg["command"],
        cfg.get("args", []),
        env=cfg.get("env") or None,
        startup_timeout_sec=60,  # Codex's 10s default is too short for the X handshake.
        tool_timeout_sec=int(
            _mcp.tool_timeout_ms(os.environ.get(_mcp.TOOL_TIMEOUT_OVERRIDE_VAR))
        )
        // 1000,
    )
    config_toml.write_text(existing + fragment, encoding="utf-8")
    logger.info("Appended [mcp_servers.%s] to %s", server_name, config_toml)
    return str(config_toml)


WEB_CLAUDE_RUNTIME_ENV = {
    # Web-only runtime tuning. Permissions and MCP grants deliberately do not live here: those
    # are rendered by core.agent_config below, exactly as on the other four platforms.
    "IS_SANDBOX": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "DISABLE_AUTOUPDATER": "1",
    "BASH_DEFAULT_TIMEOUT_MS": "600000",
    "BASH_MAX_OUTPUT_LENGTH": "10000",
    "FORCE_AUTO_BACKGROUND_TASKS": "1",
    "ENABLE_BACKGROUND_TASKS": "1",
    "CLAUDE_CODE_DISABLE_NONSTREAMING_FALLBACK": "1",
    # Model-request retry is owned by the shared proxy for both agent CLIs. Keeping
    # client retries at zero prevents nested retry budgets.
    "CLAUDE_CODE_MAX_RETRIES": "0",
    "MAX_STRUCTURED_OUTPUT_RETRIES": "0",
    "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
    # Keep model requests distinct from MCP calls: 30 minutes per model request, three
    # minutes per browser action, and 20 hours for the complete agent run.
    "API_TIMEOUT_MS": DEFAULT_API_TIMEOUT_MS,
    _mcp.TOOL_TIMEOUT_VAR: _mcp.tool_timeout_ms(
        os.environ.get(_mcp.TOOL_TIMEOUT_OVERRIDE_VAR)
    ),
}


def _write_workspace_claude_settings(
    workspace: Path,
    browser_mcp: str = DEFAULT_BROWSER_MCP,
    allow_image_read: bool = True,
) -> None:
    """Write a workspace-local .claude/settings.local.json so the agent's
    claude-code subprocess uses the intended gateway/model/permissions.

    claude-code resolves project settings from the nearest .claude/ dir, and a
    workspace-local settings.local.json overrides both an ancestor project-level
    .claude/settings*.json (which may pin a different ANTHROPIC_BASE_URL) AND
    the inherited process env. Permissions always come from core.agent_config;
    Web contributes only its selected MCP server and runtime-specific env.
    """
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "").strip()
    env = dict(WEB_CLAUDE_RUNTIME_ENV)
    # Resolve the override at call time as well as import time. Direct callers/tests may set
    # RB_MCP_TOOL_TIMEOUT after importing this module, and settings.json otherwise wins over
    # the correctly updated process environment with the stale default.
    env["API_TIMEOUT_MS"] = (
        os.environ.get("RB_API_TIMEOUT_MS", "").strip() or DEFAULT_API_TIMEOUT_MS
    )
    env[_mcp.TOOL_TIMEOUT_VAR] = _mcp.tool_timeout_ms(
        os.environ.get(_mcp.TOOL_TIMEOUT_OVERRIDE_VAR)
    )

    api_key = os.environ.get("ANTHROPIC_API_KEY", "") or os.environ.get(
        "ANTHROPIC_AUTH_TOKEN", ""
    )
    auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "") or api_key
    model = os.environ.get("ANTHROPIC_MODEL", "")

    if base_url:
        env["ANTHROPIC_BASE_URL"] = base_url
        if auth_token:
            env["ANTHROPIC_AUTH_TOKEN"] = auth_token
        if api_key:
            env["ANTHROPIC_API_KEY"] = api_key
        if model:
            for key in (
                "ANTHROPIC_MODEL",
                "ANTHROPIC_DEFAULT_OPUS_MODEL",
                "ANTHROPIC_DEFAULT_SONNET_MODEL",
                "ANTHROPIC_DEFAULT_HAIKU_MODEL",
                "CLAUDE_CODE_SUBAGENT_MODEL",
            ):
                env[key] = model

    settings = agent_config.claude_settings(
        mcp_servers=(_mcp.BROWSER_SERVER,),
        env=env,
    )

    if not allow_image_read:
        # Highest-precedence settings file the agent actually reads.
        _apply_image_ban(settings)

    claude_dir = workspace / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / "settings.local.json").write_text(
        json.dumps(settings, indent=2), encoding="utf-8"
    )
    logger.info(
        "Wrote shared workspace settings (MCP=%s, BASE_URL=%s, MODEL=%s, allow_image_read=%s)",
        _mcp.BROWSER_SERVER,
        base_url or "<inherited>",
        model,
        allow_image_read,
    )


def _build_agent_prompt(site_url: str) -> str:
    """Build the full-site recreation prompt for the served reference."""
    return recreation_prompt.render(
        "web",
        site_url=site_url,
    )


def _loopback_port(url: str):
    """(port) if `url` points at loopback, else None."""
    from urllib.parse import urlparse
    try:
        u = urlparse(url if "://" in url else "http://" + url)
    except ValueError:
        return None
    host = (u.hostname or "").lower()
    if host not in ("localhost", "127.0.0.1", "0.0.0.0", "::1"):
        return None
    return u.port or (443 if u.scheme == "https" else 80)


def _marker_path(workspace) -> Path:
    """Where the isolation verdict is recorded — outside the agent-writable tree.

    Kept as one function so the runner that writes it and the scorer that reads it
    cannot drift apart. evaluation.evaluator imports this.
    """
    workspace = Path(workspace)
    return workspace.parent / f".net_isolation-{workspace.name}"


def _build_net_isolation(workspace: Path, scaffold: str):
    """Decide whether to isolate the agent's network, and what to relay back.

    Returns (OutsideRelays|None, relay_specs, route) where route is the
    net_isolate route in force ("" = isolation off, agent has open egress).

    The reference URL is read from workspace/task.json — the file prepare_workspace
    wrote with the real one — rather than taken as an argument.

    Only LOOPBACK endpoints can be relayed: a remote model gateway would also
    need its hostname to resolve inside the namespace, and there is no DNS there,
    so rewriting the agent's base_url would be required. In managed execution the
    agent talks to a compatible local relay, so the common case is covered; a non-loopback
    endpoint raises rather than running the agent unisolated.

    FAIL-CLOSED. Every failure here raises IsolationUnavailable. The only way to
    run without isolation is to ask for it explicitly with MOCKWEB_NET_ISOLATION=0,
    because the alternative — warn and continue — is what lets a rollout with open
    egress produce a normal-looking score.
    """
    from runner import net_isolate

    route, why = net_isolate.preflight()
    if not route:
        # The ONE permitted way to run unisolated: an explicit opt-out, for local
        # development on a box with no namespace. Every other failure raises.
        if not net_isolate.isolation_requested():
            logger.warning("network isolation OFF — disabled via %s; the agent can reach "
                           "the internet and egress_scan penalties apply",
                           net_isolate.ISOLATION_ENV)
            return None, [], ""
        raise net_isolate.IsolationUnavailable(
            f"cannot isolate the agent's network ({why}). Fix the environment rather than "
            f"scoring an unisolated run: the deployment platform task config needs `privileged: true` (grants "
            f"CAP_SYS_ADMIN, so `unshare -n` works without a user namespace), or the pod "
            f"needs user.max_user_namespaces > 0. For a local run with neither, set "
            f"{net_isolate.ISOLATION_ENV}=0 to accept an unisolated rollout.")

    try:
        site_url = json.loads(
            (Path(workspace) / "task.json").read_text(encoding="utf-8")
        ).get("input", {}).get("site_url", "")
    except (OSError, ValueError) as e:
        raise net_isolate.IsolationUnavailable(
            f"cannot read site_url from {workspace}/task.json ({e}); without it the "
            f"reference site cannot be relayed into the namespace") from e

    endpoints = {}          # port -> label
    site_port = _loopback_port(site_url)
    if site_port:
        endpoints[site_port] = "reference-site"
    # Only the endpoints the ACTIVE scaffold will actually dial. Checking all three
    # made a codex run fail closed over a leftover ANTHROPIC_BASE_URL it never reads.
    _endpoint_vars = (("OPENAI_BASE_URL",) if scaffold == "codex"
                      else ("ANTHROPIC_BASE_URL", "ANTHROPIC_NATIVE_BASE_URL"))
    for var in _endpoint_vars:
        raw = os.environ.get(var, "").strip()
        if not raw:
            continue
        port = _loopback_port(raw)
        if port:
            endpoints.setdefault(port, var)
        else:
            raise net_isolate.IsolationUnavailable(
                f"{var}={raw} is not loopback, so it would be unreachable inside the "
                f"namespace (no route, no DNS) and the rollout could not reach the model. "
                f"Point the agent at a loopback model endpoint, or set "
                f"{net_isolate.ISOLATION_ENV}=0 to run unisolated on purpose.")
    if not site_port:
        raise net_isolate.IsolationUnavailable(
            f"site_url {site_url!r} is not loopback, so the reference could not be "
            f"relayed into the namespace")

    relay_dir = Path(os.environ.get(net_isolate.RELAY_DIR_ENV, "/tmp/mockweb-relays"))
    table, specs = [], []
    for port, label in sorted(endpoints.items()):
        sock = str(relay_dir / f"{label}-{port}.sock")
        table.append((port, sock, "127.0.0.1", port))
        specs.append(f"{port}={sock}")
    relays = net_isolate.OutsideRelays(table)
    try:
        relays.start()
    except Exception as e:                      # noqa: BLE001 — re-raised as fail-closed
        relays.stop()
        raise net_isolate.IsolationUnavailable(
            f"relays failed to start ({e}); the namespace would have no way to reach "
            f"the reference site or the model") from e
    logger.info("network isolation ON (%s scaffold, %s): namespace has no egress; relayed %s",
                scaffold, why, ", ".join(f"{lbl}:{p}" for p, lbl in sorted(endpoints.items())))
    return relays, specs, route
def _agent_run_prefix() -> list:
    """Command prefix that de-privileges the agent (rollout AND build).

    The reference source (``/protected/dataset/<domain>/site``), the withheld answers
    (``.../evaluation``), and the scorer must be UNREADABLE by the agent — it may reach the
    reference ONLY over HTTP (localhost). This is enforced by running the agent as a
    NON-root uid while those paths are root-owned 0700: the harness sets
    ``MOCKWEB_AGENT_RUN_PREFIX`` to a de-privilege wrapper, prepended to EVERY agent
    subprocess (the rollout in ``_spawn`` and the npm install/build in
    ``build_agent_output``). The SiteServer runs OUTSIDE this
    wrapper (unwrapped root orchestrator subprocess) so it can still read + serve the
    reference. setpriv keeps the parent env, so the prefix also fixes HOME via ``env``.

    Example (main.sh):
        MOCKWEB_AGENT_RUN_PREFIX="setpriv --reuid=agent --regid=agent --clear-groups -- \
            env HOME=/home/agent"
    Empty/unset -> no wrapper (dev/un-sealed: the agent shares the orchestrator's uid).
    """
    raw = os.environ.get("MOCKWEB_AGENT_RUN_PREFIX", "").strip()
    if not raw:
        return []
    import shlex

    try:
        return shlex.split(raw)
    except Exception as e:
        # Fail CLOSED: a configured-but-unparseable prefix means the harness intended to
        # de-privilege the agent; running unwrapped would silently expose the reference/
        # answers/scorer to a root agent. Abort the run instead of degrading to root.
        raise RuntimeError(
            f"MOCKWEB_AGENT_RUN_PREFIX is set but unparseable ({raw!r}): {e}. "
            "Refusing to run the agent un-sealed."
        )


def _browser_reference_preflight(
    browser_mcp: str,
    site_url: str,
    *,
    allow_image_read: bool = True,
) -> str:
    """Prove the recreation principal can observe the live reference site.

    This is deliberately a functional gate, not a server-start check. Playwright
    must navigate the real localhost URL and return its accessibility snapshot.
    The command is prefixed with the same uid boundary used for the rollout.
    """
    cfg = _prepare_browser_mcp_cfg(browser_mcp, allow_image_read=allow_image_read)
    child_env = os.environ.copy()
    child_env.update({str(k): str(v) for k, v in (cfg.get("env") or {}).items()})
    command = _agent_run_prefix() + [cfg["command"], *cfg.get("args", [])]

    ok, detail = mcp_probe.playwright_reference(
        command,
        site_url,
        timeout=45,
        require_image=allow_image_read,
        env=child_env,
    )

    if not ok:
        raise RuntimeError(
            f"strict {browser_mcp} reference MCP preflight failed: {detail}"
        )
    logger.info("Strict %s reference MCP preflight passed: %s", browser_mcp, detail)
    return detail


def strict_browser_preflight(
    browser_mcp: str,
    site_url: str,
    *,
    allow_image_read: bool = True,
) -> str:
    """Apply the shared strict/warn policy to Web's functional MCP probe."""
    mode = normalize_cua_preflight_mode(os.environ.get("RB_CUA_PREFLIGHT_MODE"))
    try:
        return _browser_reference_preflight(
            browser_mcp,
            site_url,
            allow_image_read=allow_image_read,
        )
    except RuntimeError as exc:
        if mode != "warn":
            raise
        detail = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "Reference MCP preflight failed; continuing because "
            "RB_CUA_PREFLIGHT_MODE=warn: %s",
            detail,
        )
        return f"warning: {detail}"


def _chown_tree_to_agent(path: Path) -> None:
    """Hand a root-created tree to the de-privileged Web agent.

    Do not follow symlinks while running as root: the workspace is agent-writable,
    and following an agent-authored link during a later handoff could change an
    unrelated target's ownership.
    """
    uid = os.environ.get("MOCKWEB_AGENT_UID", "").strip()
    if not uid:
        return
    gid = os.environ.get("MOCKWEB_AGENT_GID", uid).strip()
    uid_i, gid_i = int(uid), int(gid)
    path = Path(path)
    if path.is_symlink():
        raise RuntimeError(f"refusing to chown symlinked agent tree: {path}")
    for root, dirs, files in os.walk(path, followlinks=False):
        targets = [Path(root), *[Path(root) / name for name in dirs + files]]
        for target in targets:
            try:
                os.chown(target, uid_i, gid_i, follow_symlinks=False)
            except FileNotFoundError:
                pass


def _attest_agent_claude_state_writable(claude_dir: Path) -> None:
    """Prove the exact demoted launch principal can create Claude session state."""
    prefix = _agent_run_prefix()
    if not prefix:
        return
    session_env = Path(claude_dir) / "session-env"
    probe = session_env / f".rb-write-probe-{os.getpid()}"
    for command in (["mkdir", "-p", str(session_env)], ["touch", str(probe)]):
        result = subprocess.run(
            [*prefix, *command], capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or "no diagnostic").strip()
            raise RuntimeError(
                f"agent cannot write Claude session state under {claude_dir}: {detail}"
            )
    probe.unlink(missing_ok=True)
    logger.info("Agent Claude state write probe passed: %s", session_env)


def _chown_workspace_to_agent(workspace: Path) -> None:
    """Hand the root-created workspace to the de-privileged agent uid.

    ``prepare_workspace`` runs as root (the orchestrator), so the workspace and the
    copied template are root-owned. When the rollout/build are de-privileged (the
    harness sets ``MOCKWEB_AGENT_UID``), the agent uid must own the workspace to write
    into it. No-op when unset (dev/un-sealed root run). Never touches anything outside
    the workspace, so the root-owned reference/answers/scorer stay unreadable.
    """
    _chown_tree_to_agent(workspace)


async def _terminate_process_group(proc, grace_seconds: float = 5.0) -> None:
    """Stop a subprocess and every descendant in its dedicated process group.

    The network-isolation wrapper must remain alive to host its loopback relays,
    so it launches the CLI as a child instead of ``exec``-ing it.  Killing only
    that wrapper leaves the real Claude/Codex process running.  All callers start
    the wrapper in a new session, making its pid a safe, exact process-group id.
    """
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        await proc.wait()
        return
    except OSError:
        proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), timeout=grace_seconds)
        return
    except asyncio.TimeoutError:
        pass
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        await proc.wait()
        return
    except OSError:
        proc.kill()
    await proc.wait()


def _configured_reasoning_effort() -> str:
    """Resolve the shared five-platform reasoning-effort contract.

    deployment platform exposes the canonical ``thinking_effort`` task parameter as
    ``THINKING_EFFORT``. Keep ``REASONING_EFFORT`` as the explicit local
    override, and accept ``RB_THINKING_EFFORT`` for the shared in-pod path.
    """
    return (
        os.environ.get("REASONING_EFFORT", "").strip()
        or os.environ.get("THINKING_EFFORT", "").strip()
        or os.environ.get("RB_THINKING_EFFORT", "").strip()
    )


async def run_agent(
    model: str,
    workspace: Path,
    timeout: int = DEFAULT_AGENT_TIMEOUT_SEC,
    prompt: str = None,
    browser_mcp: str = DEFAULT_BROWSER_MCP,
    allow_image_read: bool = True,
) -> dict:
    """Run exactly one shared agent invocation.

    A no-code completion, policy refusal, or terminal API error is the outcome of
    that invocation.  In particular, Web must not grant extra model turns via a
    platform-only ``--continue``/``resume --last`` loop; infrastructure retries
    belong to deployment platform and model outcomes stay comparable across platforms.
    """
    task_json = json.loads((workspace / "task.json").read_text())
    site_url = task_json.get("input", {}).get("site_url", "http://localhost:8100/")
    scaffold = os.environ.get("SCAFFOLD", "claude-code").lower()
    capture_tool_uses = os.environ.get(
        "RB_CAPTURE_TOOL_USE_SCREENSHOTS", "false"
    ).strip().lower() in ("1", "true", "yes", "on")
    for name in (
        "MOCKWEB_CAPTURE_BROWSER_CMD_JSON",
        "MOCKWEB_CAPTURE_TRAJECTORY",
        "MOCKWEB_CAPTURE_OUTPUT_DIR",
        "MOCKWEB_CAPTURE_COMMAND_JSON",
    ):
        os.environ.pop(name, None)
    cdp_endpoint = "http://127.0.0.1:9222" if capture_tool_uses else ""
    if scaffold.startswith("codex"):
        # Codex reads only config.toml. Write and validate that exact file before the
        # functional probe so a healthy standalone MCP cannot mask missing CLI wiring.
        setup_codex_browser_mcp(browser_mcp,
            allow_image_read=allow_image_read,
            cdp_endpoint=cdp_endpoint,
        )
        mcp_config_path = ""
    else:
        # Claude receives this exact path via --mcp-config below.
        mcp_config_path = setup_browser_mcp(
            browser_mcp,
            allow_image_read=allow_image_read,
            cdp_endpoint=cdp_endpoint,
        )

    strict_browser_preflight(
        browser_mcp,
        site_url,
        allow_image_read=allow_image_read,
    )

    if prompt is None:
        prompt = _build_agent_prompt(site_url)

    log_file = workspace / "agent_run.log"
    reasoning_effort = _configured_reasoning_effort()
    trajectory_log = workspace / "trajectory.jsonl"
    stderr_log = workspace / "stderr.log"
    prompt_file = agent_invocation.write_prompt(workspace / "prompt.txt", prompt)
    cli = agent_invocation.normalize_agent_cli(scaffold)
    request_timeout_ms = int(
        os.environ.get("RB_API_TIMEOUT_MS", "").strip() or DEFAULT_API_TIMEOUT_MS
    )
    tool_timeout_ms = int(
        _mcp.tool_timeout_ms(os.environ.get(_mcp.TOOL_TIMEOUT_OVERRIDE_VAR))
    )
    invocation = agent_invocation.InvocationSpec(
        agent_cli=cli,
        # Claude Code derives its effective context window from the model alias.
        # Keep the upstream model id bare, but decorate the CLI-facing alias when
        # the common five-platform context flag is enabled.  The compaction env
        # alone does not opt Claude Code into the 1M context window.
        model=agent_invocation.normalize_model(
            cli,
            (
                model
                if cli == "codex"
                else os.environ.get("ANTHROPIC_MODEL", "")
                or agent_invocation.DEFAULT_CLAUDE_MODEL_ALIAS
            ),
            context_1m=os.environ.get("CONTEXT_1M", "").strip().lower()
            in ("1", "true", "yes", "on"),
        ),
        prompt_file=str(prompt_file),
        workspace=str(workspace),
        trajectory_path=str(trajectory_log),
        stderr_path=str(stderr_log),
        timeout_sec=timeout,
        request_timeout_ms=request_timeout_ms,
        tool_timeout_ms=tool_timeout_ms,
        mcp_config=mcp_config_path,
        reasoning_effort=reasoning_effort,
        run_name=f"rb-web-{workspace.name}",
        isolate_environment=True,
    )

    # Optional filesystem sandbox wrapper (masks the reference/answers/scorer from the agent;
    # HTTP-only access to the reference). Prepended to the rollout command. The SiteServer is
    # started outside this (unwrapped) so it can still read + serve the reference.
    _run_prefix = _agent_run_prefix()

    # Network isolation (runner.net_isolate): the agent gets a namespace with no
    # route out, and only the endpoints below are relayed back in. Where the
    # setpriv prefix sits relative to the unshare depends on the route, so
    # net_isolate.wrap_cmd composes both rather than this file.
    _netns, _relay_specs, _isolation_route = _build_net_isolation(workspace, scaffold)
    # Marker for the scorer: eval is a separate process in the deployment platform path, so this is
    # the only way evaluation.egress_scan can distinguish "clean" from "clean
    # because it had no route out".
    #
    # It goes NEXT TO the workspace, not inside it. The workspace is chowned to the
    # agent uid, and owning the directory means being able to unlink or replace
    # anything in it — so a marker kept there is a claim the graded party can edit.
    # The parent is the harness's own output dir and stays root-owned. The name
    # carries the workspace's, so concurrent local runs sharing /tmp as a parent
    # cannot overwrite each other's.
    try:
        _marker_path(workspace).write_text("1" if _isolation_route else "0",
                                           encoding="utf-8")
    except OSError as e:
        # Fatal, because the marker IS the guarantee. Isolation without a record of it
        # is scored as "unknown", and unknown is penalised — so a silent write failure
        # would cap a run that was in fact confined. Failing here keeps "isolated =>
        # never penalised for egress" true with no exceptions.
        from runner.net_isolate import IsolationUnavailable
        if _isolation_route:
            if _netns is not None:
                _netns.stop()
            raise IsolationUnavailable(
                f"isolated the agent but could not record it at {_marker_path(workspace)} "
                f"({e}); the scorer would read 'unknown' and penalise a clean run") from e
        logger.warning("could not write the isolation marker at %s: %s",
                       _marker_path(workspace), e)

    capture_root = workspace.parent / f".{workspace.name}-tool-use-screenshots"
    if capture_tool_uses:
        try:
            shutil.rmtree(capture_root, ignore_errors=True)
            capture_root.mkdir(parents=True)
            _chown_tree_to_agent(capture_root)
            chromium = _resolve_chromium_executable()
            if not chromium:
                raise RuntimeError("bundled Chromium is missing")
            browser_profile = workspace.parent / f".{workspace.name}-capture-browser"
            shutil.rmtree(browser_profile, ignore_errors=True)
            browser_profile.mkdir(parents=True)
            _chown_tree_to_agent(browser_profile)
            browser_cmd = [
                chromium,
                "--headless",
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--remote-debugging-address=127.0.0.1",
                "--remote-debugging-port=9222",
                f"--user-data-dir={browser_profile}",
                "about:blank",
            ]
            # With NETNS, this helper starts as root to configure loopback;
            # explicitly demote Chromium to the same agent principal.
            if _isolation_route != "userns":
                browser_cmd = [*_run_prefix, *browser_cmd]
            os.environ["MOCKWEB_CAPTURE_BROWSER_CMD_JSON"] = json.dumps(browser_cmd)
            os.environ["MOCKWEB_CAPTURE_TRAJECTORY"] = str(
                browser_profile / "live-trajectory.jsonl"
            )
            os.environ["MOCKWEB_CAPTURE_OUTPUT_DIR"] = str(capture_root)
            os.environ["MOCKWEB_CAPTURE_COMMAND_JSON"] = json.dumps(
                [
                    "node",
                    str(Path(__file__).resolve().with_name("capture_browser.js")),
                    cdp_endpoint,
                    "--stdio",
                ]
            )
        except Exception as exc:  # noqa: BLE001 - optional capture is fail-open
            logger.warning("tool-use capture disabled after setup failure: %s", exc)
            capture_tool_uses = False
            for name in (
                "MOCKWEB_CAPTURE_BROWSER_CMD_JSON",
                "MOCKWEB_CAPTURE_TRAJECTORY",
                "MOCKWEB_CAPTURE_OUTPUT_DIR",
                "MOCKWEB_CAPTURE_COMMAND_JSON",
            ):
                os.environ.pop(name, None)
            shutil.rmtree(capture_root, ignore_errors=True)
            shutil.rmtree(
                workspace.parent / f".{workspace.name}-capture-browser",
                ignore_errors=True,
            )

    def _confine(run_cmd):
        from runner.net_isolate import wrap_cmd
        return wrap_cmd(run_cmd, _relay_specs, _isolation_route, _run_prefix)

    async def _spawn(run_spec, run_timeout):
        append = run_spec.session_mode != "initial"
        if capture_tool_uses:
            for path in (trajectory_log, stderr_log):
                path.parent.mkdir(parents=True, exist_ok=True)
                if not append:
                    path.write_bytes(b"")
                elif path.is_file() and path.stat().st_size:
                    with path.open("rb+") as stream:
                        stream.seek(-1, os.SEEK_END)
                        if stream.read(1) != b"\n":
                            stream.seek(0, os.SEEK_END)
                            stream.write(b"\n")
        full_cmd = _confine(agent_invocation.build_argv(run_spec))
        try:
            proc = await asyncio.create_subprocess_exec(
                *full_cmd,
                cwd=str(workspace),
                env=agent_invocation.runtime_env(run_spec),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
        except FileNotFoundError:
            logger.error(
                "%s CLI not found (or run-prefix %r missing): %s",
                scaffold,
                _run_prefix,
                full_cmd[:2],
            )
            return {"status": "not_available", "returncode": -1, "stdout": b""}
        async def _pump(reader, destinations):
            size = 0
            while True:
                chunk = await reader.read(65536)
                if not chunk:
                    break
                size += len(chunk)
                for destination in destinations:
                    destination.write(chunk)
                    destination.flush()
            return size

        try:
            prompt_bytes = Path(run_spec.prompt_file).read_bytes()
            if not capture_tool_uses:
                communicate = asyncio.create_task(proc.communicate(input=prompt_bytes))
                out, err = await asyncio.wait_for(
                    asyncio.shield(communicate), timeout=run_timeout
                )
                return {
                    "status": "success" if proc.returncode == 0 else "error",
                    "returncode": agent_invocation.normalize_native_rc(proc.returncode),
                    "output_length": len(out or b""),
                    "stdout": out or b"",
                    "stderr": err or b"",
                }
            # Capture must tail bytes while the live page still exists.  The
            # default-off path above deliberately keeps the prior communicate()
            # behavior byte-for-byte.
            assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
            proc.stdin.write(prompt_bytes)
            await proc.stdin.drain()
            proc.stdin.close()
            with contextlib.ExitStack() as stack:
                trajectory_stream = stack.enter_context(trajectory_log.open("ab"))
                stderr_stream = stack.enter_context(stderr_log.open("ab"))
                stdout_destinations = [trajectory_stream]
                if cli == "claude":
                    stdout_destinations.append(
                        stack.enter_context(log_file.open("ab" if append else "wb"))
                    )
                stdout_task = asyncio.create_task(
                    _pump(proc.stdout, stdout_destinations)
                )
                stderr_task = asyncio.create_task(_pump(proc.stderr, [stderr_stream]))
                wait_task = asyncio.create_task(proc.wait())
                completed = asyncio.gather(wait_task, stdout_task, stderr_task)
                _returncode, out_size, _stderr_size = await asyncio.wait_for(
                    asyncio.shield(completed), timeout=run_timeout
                )
        except asyncio.TimeoutError:
            await _terminate_process_group(proc)
            # Drain bytes already emitted before the kill so a timeout keeps its
            # partial trajectory instead of looking like a process that never ran.
            # A broken isolation wrapper can retain a pipe after the process group
            # is gone, so the drain itself must not become a second unbounded wait.
            try:
                if not capture_tool_uses:
                    out, err = await asyncio.wait_for(communicate, timeout=10)
                else:
                    _returncode, out_size, _stderr_size = await asyncio.wait_for(
                        completed, timeout=10
                    )
            except asyncio.TimeoutError:
                if not capture_tool_uses:
                    communicate.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await communicate
                    out, err = b"", b""
                else:
                    for task in (stdout_task, stderr_task):
                        task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await asyncio.gather(stdout_task, stderr_task)
                    out_size = 0
            if not capture_tool_uses:
                return {
                    "status": "timeout",
                    "returncode": agent_invocation.TIMEOUT_EXIT_CODE,
                    "stdout": out or b"",
                    "stderr": err or b"",
                }
            return {
                "status": "timeout",
                "returncode": agent_invocation.TIMEOUT_EXIT_CODE,
                "output_length": out_size,
            }
        return {
            "status": "success" if proc.returncode == 0 else "error",
            "returncode": agent_invocation.normalize_native_rc(proc.returncode),
            "output_length": out_size,
        }

    def _persist(run_result, tag):
        if capture_tool_uses:
            # _spawn persists streams in real time so capture can react while
            # the live page still exists.
            return
        out = run_result.get("stdout") or b""
        err = run_result.get("stderr") or b""
        # Preserve the pre-option behavior when capture is disabled.
        if out or not tag:
            agent_invocation.persist_stream(trajectory_log, out, append=bool(tag))
            if cli == "claude":
                agent_invocation.persist_stream(log_file, out, append=bool(tag))
        if err or not tag:
            agent_invocation.persist_stream(stderr_log, err, append=bool(tag))

    agent_t0 = time.time()
    try:
        result = await _spawn(invocation, timeout)
        _persist(result, "")
        if result["status"] == "not_available":
            result.pop("stdout", None)
            return result

        result.pop("stdout", None)
        result.pop("stderr", None)
        verdict = agent_invocation.terminal_result(
            trajectory_log,
            result.get("returncode", agent_invocation.INFRA_EXIT_CODE),
            timed_out=result.get("status") == "timeout",
        )
        result["native_returncode"] = verdict.native_rc
        result["returncode"] = verdict.exit_code
        result["agent_verdict"] = verdict.status
        result["protocol_status"] = verdict.protocol_status
        api_failure = agent_invocation.terminal_api_failure(trajectory_log)
        if api_failure:
            result["api_error"] = api_failure
        result["status"] = {
            agent_invocation.TERMINAL_COMPLETED: "success",
            agent_invocation.TERMINAL_BUDGET_EXHAUSTED: "timeout",
            agent_invocation.TERMINAL_TERMINATED: "terminated",
        }.get(verdict.status, "error")
        result["sessions"] = _collect_sessions(workspace, agent_t0)
        if capture_tool_uses and capture_root.is_dir():
            result["tool_use_screenshots"] = str(capture_root)
        # Recorded so the integrity gate can tell "clean" from "clean because it could
        # not get out" — see evaluation.egress_scan.isolation_active.
        result["net_isolation"] = _isolation_route or False
        return result
    finally:
        if _netns is not None:
            _netns.stop()
        for name in (
            "MOCKWEB_CAPTURE_BROWSER_CMD_JSON",
            "MOCKWEB_CAPTURE_TRAJECTORY",
            "MOCKWEB_CAPTURE_OUTPUT_DIR",
            "MOCKWEB_CAPTURE_COMMAND_JSON",
        ):
            os.environ.pop(name, None)
        if capture_tool_uses:
            shutil.rmtree(
                workspace.parent / f".{workspace.name}-capture-browser",
                ignore_errors=True,
            )


def _collect_sessions(workspace: Path, since_ts: float) -> int:
    """Copy the agent's own session transcripts next to trajectory.jsonl.

    Both CLIs: Claude Code writes its transcript under a projects dir, codex writes its
    "rollout" under CODEX_HOME/sessions. web captured only the stream-json trajectory and threw
    the native transcripts away; the codex half was missing on every platform. The transcript is
    what carries the pre-compaction history and the subagent splits, so without it a run cannot
    be replayed the way the others can.

    The agent runs de-privileged as `agent` (MOCKWEB_AGENT_RUN_PREFIX sets HOME=/home/agent),
    so its projects dir is there, not under root's HOME. Best-effort: a missing dir or an
    unreadable file is skipped, never raised — losing a transcript must not fail a scored run.
    """
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from core import trajectory
    except Exception as exc:  # pragma: no cover - import guard
        logger.warning("session collect skipped (core.trajectory unavailable): %s", exc)
        return 0
    # agent_session_dirs, not a hand-built ~/.claude/projects list: codex keeps its transcript
    # ("rollout") under $CODEX_HOME/sessions instead, and web runs codex too — hand-listing one
    # CLI's dir is how every platform silently shipped zero codex transcripts.
    dirs = []
    homes = []
    for home, label in (
        ("/home/agent", "agent"),
        (os.path.expanduser("~"), "main"),
        ("/root", "root"),
    ):
        if home not in {h for h, _ in homes}:
            homes.append((home, label))
    for home, label in homes:
        # Pass the matching Codex home explicitly. A process-level CODEX_HOME
        # would otherwise make multiple user homes all point at one sessions/
        # tree and duplicate every rollout under different labels.
        for d, lbl in trajectory.agent_session_dirs(
            home, label, codex_home=str(Path(home) / ".codex")
        ):
            if Path(d).is_dir():
                dirs.append((d, lbl))
    if not dirs:
        logger.info("no native agent transcript dirs; preserving the stream copy only")
    stream = workspace / "trajectory.jsonl"
    written = trajectory.collect_local(
        dirs,
        workspace / "sessions",
        since_ts=since_ts,
        stage="recreation",
        stream_src=str(stream) if stream.is_file() else None,
    )
    logger.info(
        "collected %d session transcript(s) -> %s", len(written), workspace / "sessions"
    )
    return len(written)


def _build_commands(workspace: Path) -> list[tuple[str, list[str]]]:
    """Return the deterministic, network-free build command sequence."""
    steps = []
    if not (Path(workspace) / "node_modules").is_dir():
        steps.append(
            ("install", ["npm", "install", "--offline", "--no-audit", "--no-fund"])
        )
    steps.append(("build", ["npm", "run", "build"]))
    return steps


async def build_agent_output(workspace: Path) -> dict:
    """Build the agent's React project and copy output.

    The workspace root IS the React project (package.json, src/, etc.).
    Uses the image-pinned dependency tree when present; otherwise performs a
    strictly offline npm install, then copies dist/index.html to output/.
    """
    workspace = Path(workspace)

    # De-privilege the build exactly like the rollout: npm runs agent-authored code
    # (postinstall hooks, vite config/plugins), so it must NOT run as root while the
    # reference/answers/scorer are on disk. Same prefix as _spawn (setpriv + env HOME).
    _run_prefix = _agent_run_prefix()

    # The build runs in an isolated namespace with NO relays at all: with
    # node_modules shipped and install forced offline it has no legitimate
    # outbound call, so this is the one place the network can be closed
    # completely. It also stops a malicious postinstall/vite plugin — which runs
    # arbitrary agent-authored code — from phoning home after the rollout ended.
    from runner import net_isolate
    _build_route, _build_why = net_isolate.preflight()
    if _build_route:
        logger.info("build runs with no network at all (isolated %s, no relays)", _build_why)
    elif net_isolate.isolation_requested():
        # Same reasoning as the rollout: npm here executes agent-authored postinstall
        # and vite-plugin code, so an unisolated build must not silently ship a dist.
        raise net_isolate.IsolationUnavailable(
            f"cannot isolate the build's network ({_build_why})")
    else:
        logger.warning("build network NOT closed — disabled via %s", net_isolate.ISOLATION_ENV)

    def _confine_build(cmd):
        # route "" degrades to de-privilege only, so no branch here.
        return net_isolate.wrap_cmd(cmd, [], _build_route, _run_prefix)

    # Local/dev fallback may use an already-warm npm cache, but it is explicitly
    # offline. Release jobs skip install because prepare_workspace linked the
    # image-pinned dependency tree.
    for step_name, step_cmd in _build_commands(workspace):
        try:
            process = await asyncio.create_subprocess_exec(
                *_confine_build(step_cmd),
                cwd=str(workspace),
                env=agent_invocation.isolated_agent_environment(),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            step_timeout = 600 if step_name == "install" else 300
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=step_timeout
            )
            if process.returncode != 0:
                return {
                    "status": f"{step_name}_failed",
                    "error": (
                        stderr.decode("utf-8", errors="replace")[:500] if stderr else ""
                    ),
                }
        except asyncio.TimeoutError:
            await _terminate_process_group(process)
            return {"status": f"{step_name}_timeout"}
        except FileNotFoundError:
            return {"status": "npm_not_found"}

    dist_html = workspace / "dist" / "index.html"
    output_html = workspace / "output" / "index.html"
    output_html.parent.mkdir(parents=True, exist_ok=True)

    if dist_html.exists():
        shutil.copy2(str(dist_html), str(output_html))
        for f in (workspace / "dist").rglob("*"):
            if f.is_file() and f != dist_html:
                dest = workspace / "output" / f.relative_to(workspace / "dist")
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(str(f), str(dest))
        return {"status": "success", "output": str(output_html)}
    else:
        return {"status": "no_output", "error": "dist/index.html not found"}


# --- No-submission diagnostics (refused / api_error / no_code) ---------------------------
# Consulted ONLY when the workspace authored NOTHING (src == template). The reason is retained
# for diagnosis, but every completed no-submission outcome is scored 0 and stays in the
# denominator, including an explicit model refusal:
#   * api_error : the API/infra failed (timeouts, rate-limit, 5xx, connection resets) so the
#                 model never actually responded (e.g. huggingface.co: agent_run.log is just
#                 "API Error: The operation timed out." x5). NOT a refusal — scored 0.
#   * no_code   : the model ran but delivered nothing for a non-refusal reason (premature stop,
#                 clarifying question, hallucinated "done", tool-call-as-text). NOT a refusal —
#                 scored 0. ("no_code is not necessarily a refusal.")
# REFUSAL patterns are deliberately HIGH-PRECISION: an explicit decline VERB bound to the task.
# Bare "copyright"/"scrape"/"clone" are intentionally NOT markers — a model that merely REASONS
# about copyright and then proceeds to build authored code (bill.com, community.aws, postgresql
# in the qwen-3.7 run: scores 0.17-0.38) must NOT be mislabeled a refusal. Those runs authored
# code and never reach this path anyway; keeping the verbs strict is the second line of defence.
_REFUSAL_RES = [
    # (0) first-person decline of HELP/ASSISTANCE. DECLINE-INTENT verbs ONLY (help/assist/comply/
    # fulfil) — NOT capability verbs (complete/build/do/continue/proceed/generate), which are
    # ordinary give-up / infra-failure phrasing ("I cannot continue", "I can't complete it in
    # time", "I cannot generate the site, the fixtures failed") and are no_code/api_error, not
    # refusals. The `i` prefix accepts "I", "I'm", "I am".
    re.compile(
        r"\bi(?:\s+am|'m|\s+)\s*(?:can(?:no|')?t|cannot|will\s+not|won'?t|refuse\s+to|"
        r"(?:am\s+|'m\s+)?(?:unable|not\s+able|not\s+willing|not\s+going)\s+to)\s+"
        r"(?:help|assist|comply|fulfil|fulfill)\b",
        re.I,
    ),
    # (1) explicit "I (must|need to|will) decline/refuse"
    re.compile(
        r"\bi\s+(?:must|have\s+to|need\s+to|will|'ll|am\s+going\s+to|'m\s+going\s+to)"
        r"\s+(?:respectfully\s+)?(?:decline|refuse)\b",
        re.I,
    ),
    # (2) "I decline/refuse this/the task/to ..."
    re.compile(r"\bi\s+(?:decline|refuse)\s+(?:this|the|to|your)\b", re.I),
    # (3) "(cannot|unable to|refuse to) (comply with|help/assist ... with) this/that/the task"
    re.compile(
        r"\b(?:cannot|can'?t|unable\s+to|will\s+not|won'?t|refuse\s+to)\s+"
        r"(?:comply\s+with|help\s+(?:you\s+)?with|assist\s+(?:you\s+)?with)\s+"
        r"(?:this|that|the|your)\b",
        re.I,
    ),
    # (4) "I'm not comfortable/willing to help/build/reproduce/copy ..."
    re.compile(
        r"\bi'?m\s+not\s+(?:comfortable|willing|prepared)\s+(?:to\s+)?"
        r"(?:help|assist|comply|creat|build|reproduc|copy|clon|do\s+this|proceed)",
        re.I,
    ),
    # (5) "I cannot in good conscience / ethically ..."
    re.compile(
        r"\bi\s+(?:cannot|can'?t|won'?t)\s+(?:in\s+good\s+conscience|ethically)\b", re.I
    ),
    # (6) CJK: explicit decline of ASSISTANCE or an explicit 拒绝 — NOT neutral capability verbs
    # (完成/继续/执行/处理/构建/生成/制作 = "complete/continue/…", which are give-up, not refusal).
    re.compile(
        r"(?:我)?(?:无法|不能|不便|恕(?:难|我))(?:为你|为您|帮你|帮您|向你|向您)?"
        r"(?:协助|帮助|帮忙|提供帮助|提供协助)"
    ),
    re.compile(
        r"(?:我(?:必须|只能|不得不|选择|决定)?\s*)?拒绝"
        r"(?:此|这|该|你的|完成|执行|协助|帮助|提供|该任务|这个任务|这项任务|这一(?:请求|任务))"
    ),
    re.compile(r"恕(?:难从命|难以协助|无法协助|不能协助|不便协助)"),
]

def _agent_log_text(workspace: Path) -> str:
    """The rollout transcript, whichever scaffold produced it.

    claude-code tees stdout to agent_run.log; codex writes its --json stream to
    trajectory.jsonl and leaves no agent_run.log. Reading only the former made every
    codex api_error look like no_code, which monitor keeps instead of retrying.
    """
    best = ""
    for name in ("agent_run.log", "trajectory.jsonl"):
        p = Path(workspace) / name
        try:
            if p.exists():
                text = p.read_text(errors="replace")
                if len(text) > len(best):
                    best = text
        except OSError:
            continue
    return best


def _final_turn_api_failure(trajectory_path: Path):
    """Delegate final protocol/API classification to the shared contract."""
    return agent_invocation.terminal_api_failure(trajectory_path)


def _authored_something(workspace: Path) -> bool:
    """True iff the agent wrote or modified any file under ``src/`` beyond the
    pristine template (``.gitkeep`` placeholders ignored).

    A run that authored nothing leaves ``src/`` byte-identical to the template.
    The template still ``npm run build``s into a "Ready to build" placeholder that
    would otherwise score ~0.07, so callers treat "authored nothing" as a
    non-submission and score it 0. Partial runs that wrote real code but never
    wired ``App.tsx`` DID author something and keep their (low) score.
    """
    ws_src = Path(workspace) / "src"
    tpl_src = TEMPLATE_DIR / "src"
    if not ws_src.exists():
        return False
    tpl = {}
    if tpl_src.exists():
        for p in tpl_src.rglob("*"):
            if p.is_file():
                tpl[p.relative_to(tpl_src)] = p.read_bytes()
    for f in ws_src.rglob("*"):
        if not f.is_file() or f.name == ".gitkeep":
            continue
        rel = f.relative_to(ws_src)
        base = tpl.get(rel)
        if base is None or f.read_bytes() != base:
            return True
    return False


def _classify_no_submission(workspace: Path) -> str:
    """Sub-classify an authored-nothing run into ``refused`` or ``no_code``.

    - 'refused'   : the MODEL explicitly declined the task (high-precision decline language).
                    Diagnostic only; it is scored 0 like every other no-submission outcome.
    - 'no_code'   : any other empty result (premature stop, clarifying question, hallucinated
                    completion). Scored 0, NOT excluded. no_code is NOT necessarily a refusal.

    API failures are intentionally absent here: only the shared final protocol parser may
    classify one. Counting matching log lines used to misclassify recovered retries as infra.
    """
    text = _agent_log_text(workspace)
    if not text:
        return "no_code"
    # Explicit model refusal — high-precision decline-intent language.
    if any(r.search(text) for r in _REFUSAL_RES):
        return "refused"
    return "no_code"


async def run_single_task(
    dataset_dir: Path,
    domain: str,
    agent_model: str,
    workspace_dir: Path,
    skip_agent: bool = False,
    browser_mcp: str = DEFAULT_BROWSER_MCP,
    timeout_multiplier: float = DEFAULT_TIMEOUT_MULTIPLIER,
    skip_eval: bool = False,
    vlm_overrides: dict = None,
    allow_image_read: bool = None,
    reference_candidate: bool = False,
) -> dict:
    """Run the full pipeline for a single task.

    skip_eval: if True, run the agent + build but SKIP the scoring/evaluation pipeline
        (status="agent_only", final_score=None). Useful for generating rollouts to score
        later or out-of-band.
    vlm_overrides: optional runtime overrides for the VLM-judge sub-config (enabled /
        model / backend / base_url / api_key / mode / ...), forwarded to evaluate_task.
        These win over the baked eval_config.json and DEFAULT_EVAL_CONFIG. None ->
        leave the loaded config untouched (VLM judge off by default).
    reference_candidate: grade the frozen ``site/`` tree directly. This is an
        evaluator/fixture check: no agent, template build, or submission gate runs.
    """
    if allow_image_read is None:
        allow_image_read = image_read_allowed()

    dataset_dir = Path(dataset_dir)
    task_dir = dataset_dir / domain

    task_json_path = task_dir / "task.json"
    if not task_json_path.exists():
        return {"domain": domain, "status": "error", "error": "task.json not found"}

    task_json = json.loads(task_json_path.read_text())

    site_dir = task_dir / "site"

    result = {
        "domain": domain,
        "task_id": task_json.get("task_id", ""),
        "model": agent_model,
    }

    if reference_candidate:
        # The purpose of this lane is to validate the frozen reference against its
        # own withheld evaluator.  Do not copy it through the recreation template:
        # doing so would introduce npm/build behavior into what should be a pure
        # test-fixture check and could change the candidate being graded.
        workspace = Path(workspace_dir)
        workspace.mkdir(parents=True, exist_ok=True)
        result.update(
            {
                "submitted": True,
                "reference_candidate": True,
                "build_result": {"status": "not_applicable", "candidate": "reference"},
            }
        )
        from evaluation.test_runner import EvaluationSetupError

        try:
            from evaluation.evaluator import evaluate_task

            eval_result = await evaluate_task(
                task_dir=task_dir,
                agent_output_dir=site_dir,
                output_dir=workspace / "eval_results",
                vlm_overrides=vlm_overrides,
                reference_candidate=True,
            )
            result["scores"] = eval_result.get("scores", {})
            result["final_score"] = eval_result.get("final_score", 0.0)
            result["status"] = "success"
        except EvaluationSetupError:
            raise
        except Exception as e:
            logger.error("Reference evaluation failed for %s: %s", domain, e)
            result["status"] = "eval_error"
            result["final_score"] = 0.0
            result["eval_error"] = str(e)
        return result

    async with SiteServer(str(site_dir)) as site_server:
        workspace = Path(workspace_dir)
        if not skip_agent:
            workspace = prepare_workspace(
                workspace_dir,
                task_json,
                site_server.url,
                allow_image_read=allow_image_read,
                browser_mcp=browser_mcp,
            )
            _chown_workspace_to_agent(workspace)
            base_timeout = task_json.get(
                "time_limit_seconds", DEFAULT_TASK_TIMEOUT_SEC
            )
            effective_timeout = int(base_timeout * timeout_multiplier)
            prompt = _build_agent_prompt(site_server.url)
            rollout = await run_agent(
                model=agent_model,
                workspace=workspace,
                timeout=effective_timeout,
                prompt=prompt,
                browser_mcp=browser_mcp,
                allow_image_read=allow_image_read,
            )
            result["rollout"] = rollout

            if rollout["status"] == "not_available":
                result["status"] = "agent_not_available"
                result["final_score"] = 0.0
                return result
            if rollout.get("agent_verdict") == agent_invocation.TERMINAL_TERMINATED:
                result["status"] = "agent_terminated"
                result["final_score"] = 0.0
                result["submitted"] = _authored_something(workspace)
                return result
            if rollout.get("agent_verdict") == agent_invocation.TERMINAL_INFRA_ERROR:
                result["status"] = (
                    "api_error" if rollout.get("api_error") else "agent_error"
                )
                result["final_score"] = 0.0
                result["submitted"] = _authored_something(workspace)
                if rollout.get("api_error"):
                    result["api_error"] = rollout["api_error"]
                return result

        build_result = await build_agent_output(workspace)
        result["build_result"] = build_result

        if build_result["status"] != "success":
            logger.warning(
                "Build failed for %s: %s", domain, build_result.get("error", "")
            )
            if not (workspace / "output" / "index.html").exists():
                result["status"] = "build_failed"
                result["final_score"] = 0.0
                return result

    # Non-submission gate: if the agent authored nothing (workspace src ==
    # template), the build produced only the placeholder page. Flag it and score
    # it 0 rather than letting the placeholder earn the ~0.07 quality floor.
    submitted = _authored_something(workspace)
    result["submitted"] = submitted
    if not submitted:
        result["no_submission_reason"] = _classify_no_submission(workspace)

    # An upstream API failure that ENDED the rollout is infra, not a capability result,
    # even when the agent had already authored part of the site: grading the half-built
    # deliverable yields a plausible low score that nothing retries and nothing flags.
    # Budget exhaustion (rollout timeout) is NOT swept in — a funded run that got
    # truncated produced a real, if low, result.
    api_failure = _final_turn_api_failure(workspace / "trajectory.jsonl")
    if api_failure:
        result["api_error"] = api_failure
        if not submitted:
            result["no_submission_reason"] = "api_error"

    if skip_eval:
        result["status"] = "agent_only"
        result["final_score"] = None
        result["output_dir"] = str(workspace / "output")
        logger.info("Skipping evaluation for %s (--skip-eval)", domain)
        return result

    if api_failure and submitted:
        result["status"] = "api_error"
        result["final_score"] = 0.0
        logger.warning(
            "Rollout for %s ended on an upstream API failure (%s); scoring 0 as "
            "api_error rather than grading a half-built site",
            domain,
            api_failure,
        )
        return result

    if not submitted:
        result["status"] = "no_submission"
        result["final_score"] = 0.0
        logger.warning(
            "No submission for %s (src == template, reason=%s); scoring 0",
            domain,
            result["no_submission_reason"],
        )
        return result

    from evaluation.test_runner import EvaluationSetupError

    try:
        from evaluation.evaluator import evaluate_task

        eval_result = await evaluate_task(
            task_dir=task_dir,
            agent_output_dir=workspace / "output",
            output_dir=workspace / "eval_results",
            vlm_overrides=vlm_overrides,
        )
        result["scores"] = eval_result.get("scores", {})
        result["final_score"] = eval_result.get("final_score", 0.0)
        result["status"] = "success"
    except EvaluationSetupError:
        # Missing evaluator assets/dependencies are benchmark infrastructure
        # failures, not a model score of zero.  Let the CLI return non-zero so
        # deployment adapter excludes the job instead of averaging it into the result.
        raise
    except Exception as e:
        logger.error("Evaluation failed for %s: %s", domain, e)
        result["status"] = "eval_error"
        result["final_score"] = 0.0
        result["eval_error"] = str(e)

    return result


if __name__ == "__main__":
    from evaluation.test_runner import EvaluationSetupError

    from runner.net_isolate import IsolationUnavailable

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

    parser = argparse.ArgumentParser(description="Run one RecreationBench Web task")
    parser.add_argument("--dataset", required=True, help="Path to the released Web dataset")
    parser.add_argument("--domain", required=True, help="Site domain name")
    parser.add_argument("--model", default="claude-sonnet-4-6", help="Agent model")
    parser.add_argument("--workspace", default=None, help="Workspace directory")
    parser.add_argument("--skip-agent", action="store_true", help="Skip agent run")
    parser.add_argument(
        "--reference-candidate",
        action="store_true",
        help="Evaluate the frozen reference site directly (no agent or template build)",
    )
    parser.add_argument(
        "--skip-eval",
        action="store_true",
        help="Run the agent + build but skip the scoring/eval pipeline",
    )
    parser.add_argument(
        "--timeout-multiplier",
        type=float,
        default=DEFAULT_TIMEOUT_MULTIPLIER,
        help="Multiply task time limits by this factor (default: 10, or 20h for release tasks)",
    )
    parser.add_argument(
        "--browser-mcp",
        default=DEFAULT_BROWSER_MCP,
        choices=sorted(BROWSER_MCP_CONFIGS.keys()),
        help=f"Browser MCP server to use (default: {DEFAULT_BROWSER_MCP})",
    )
    parser.add_argument(
        "--allow-image-read",
        default=image_read_allowed(),
        action=argparse.BooleanOptionalAction,
        help="Allow the model to ingest image pixels (Read on images / screenshot "
        "inline). Default from ALLOW_IMAGE_READ env (true). Use "
        "--no-allow-image-read for text-only eval: screenshots still work but "
        "pixel reads are gracefully blocked and the rollout continues.",
    )
    # ── VLM judge (visual-fidelity LLM judge) — OFF by default ──
    # When --vlm-judge is absent the judge is force-disabled (overriding any baked
    # eval_config.json), and the visual dimension is scored by SSIM/LPIPS/Layout-IoU.
    parser.add_argument(
        "--vlm-judge",
        action="store_true",
        default=False,
        help="Enable the VLM visual-fidelity judge (default: off)",
    )
    parser.add_argument(
        "--vlm-model",
        default=None,
        help="VLM judge model name (required when the judge is enabled)",
    )
    parser.add_argument(
        "--vlm-backend",
        default=None,
        choices=["openai-compatible"],
        help="Shared VLM judge transport",
    )
    parser.add_argument("--vlm-base-url", default=None, help="VLM judge API base URL")
    parser.add_argument("--vlm-api-key", default=None, help="VLM judge API key")
    parser.add_argument(
        "--vlm-mode",
        default=None,
        choices=["assertion", "comparison"],
        help="VLM judge mode",
    )
    parser.add_argument(
        "--vlm-max-concurrency",
        type=int,
        default=vlm_judge_max_concurrency(),
        help=(
            "VLM judge max concurrent requests "
            "(default: VLM_JUDGE_MAX_CONCURRENCY, otherwise evaluator default)"
        ),
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s"
    )

    # Build the authoritative VLM-judge override. `enabled` is ALWAYS set (from the
    # flag, default False) so the CLI decision wins over any baked eval_config.json;
    # optional fields are only included when provided.
    vlm_overrides = {"enabled": bool(args.vlm_judge)}
    for _key, _val in (
        ("model", args.vlm_model),
        ("backend", args.vlm_backend),
        ("base_url", args.vlm_base_url),
        ("api_key", args.vlm_api_key),
        ("mode", args.vlm_mode),
    ):
        if _val:  # non-empty string only
            vlm_overrides[_key] = _val
    if args.vlm_max_concurrency is not None:
        vlm_overrides["max_concurrency"] = args.vlm_max_concurrency

    workspace = (
        Path(args.workspace)
        if args.workspace
        else Path(recreation_paths.POSIX_WORKSPACE_ROOT)
    )

    try:
        result = asyncio.run(
            run_single_task(
                Path(args.dataset),
                args.domain,
                args.model,
                workspace,
                skip_agent=args.skip_agent,
                browser_mcp=args.browser_mcp,
                timeout_multiplier=args.timeout_multiplier,
                skip_eval=args.skip_eval,
                vlm_overrides=vlm_overrides,
                allow_image_read=args.allow_image_read,
                reference_candidate=args.reference_candidate,
            )
        )
    except IsolationUnavailable as e:
        print(f"FATAL: network isolation is required and could not be established.\n{e}",
              file=sys.stderr)
        sys.exit(exit_contract.RC_INFRA)
    except EvaluationSetupError as e:
        print(f"FATAL: Web evaluator setup is incomplete.\n{e}", file=sys.stderr)
        sys.exit(exit_contract.RC_INFRA)

    print(json.dumps(result, indent=2, default=str))
    if result.get("status") == "agent_terminated":
        sys.exit(exit_contract.RC_TIMEOUT)
    if result.get("status") in ("agent_not_available", "agent_error", "api_error"):
        sys.exit(exit_contract.RC_INFRA)
