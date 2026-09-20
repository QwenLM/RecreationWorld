#!/usr/bin/env python3
"""Scoped interaction: what a task lets the agent do to its target.

From ``unified-agent-infra.md`` §5. A scope has four dimensions:

    Target       which app / desktop / device / browser context may be driven
    Observation  which windows, controls and pages may be seen
    Action       which inputs and control verbs may be issued
    Lifetime     which run the capability belongs to, and when it dies

This module implements the one dimension that is enforceable today WITHOUT a new component --
Observation -- and records the measured state of the other three, so the gap is explicit instead of
implied. ``dimensions()`` is that record; it is deliberately data rather than prose so a
conformance test can read it.

WHY OBSERVATION FIRST, and why it is not cosmetic
-------------------------------------------------
The driver already has a capture scope, and it already defaults to the NARROW value ("window").
What it does not have is a boundary: the scope is settable by the agent through the driver's own
``set_config`` tool, and the driver's error text tells the agent how to widen it --

    get_desktop_state requires capture_scope="desktop" (current scope is "window"). Full-display
    capture is a desktop-scope operation; call set_config with capture_scope=desktop first

-- observed in a Windows benchmark run where an agent did reach for it. So the
narrow default is a convenience, not a limit, which is precisely what §5
forbids: "Agent 只能使用已授予的 scope,不能自行扩大目标范围".

HOW IT IS ENFORCED
------------------
The MCP deny list -- the mechanism this repo already relies on, because deny is the half that takes
effect (allow is inert under ``-p``). Per-tool deny is verified by the Android
denies one individual MCP tool by full name at ``scripts/android/stages/pipeline.sh:1641``.

BENCHMARK IMPACT
----------------
Neither denied tool appears in the recreation prompt's tool catalogue (``core/recreation_prompt.py``
-- launch_app, bring_to_front, get_window_state, list_windows, kill_app, click, double_click,
right_click, type_text, press_key, hotkey, scroll, set_value). The catalogue is the documented
surface; these two are undocumented reach. So no prompt changes and nothing the agent was told it
had is taken away.

``list_windows`` is NOT denied even though it enumerates every window, because it IS in the
catalogue: denying it would change the instruction the model is scored against, which is a
benchmark change needing its own A/B. It is recorded as a known Observation gap instead.
"""

from __future__ import annotations

APPLICATION = "application"  # RecreationBench Desktop: one reference/candidate app
# The whole GUI session of one disposable sandbox. Named for what it binds -- a session, not "a
# machine" -- because the boundary that matters is sandbox/session membership: not the Host, not
# another run, not a second login session or a Windows secure desktop.
DESKTOP_SESSION = "desktop_session"
DESKTOP = DESKTOP_SESSION  # the older spelling, kept so existing callers keep working
ANDROID_APP = "android_app"
BROWSER_CONTEXT = "browser_context"

SCOPES: tuple[str, ...] = (APPLICATION, DESKTOP_SESSION, ANDROID_APP, BROWSER_CONTEXT)

# Tools whose reach is the whole machine rather than one application. Keyed by MCP server name.
_WIDENS_OBSERVATION: dict[str, tuple[str, ...]] = {
    "desktop-control": (
        # Full-display capture. Widening ONLY relative to a single application -- under session
        # scope it is a granted capability, so this list is empty there by design. set_config used
        # to live here; it moved to _MUTATES_CONFIG because it is a configuration grant, not reach.
        "get_desktop_state",
    ),
}

# Scopes for which the whole session IS the task scope, so full-session observation is a GRANT
# rather than a leak. Confirmed as the RecreationBench Desktop default: the boundary is the
# disposable VM and its GUI session, not the reference application, so narrowing to one app would
# be a different benchmark rather than better isolation.
_MACHINE_IS_THE_SCOPE: frozenset[str] = frozenset({DESKTOP_SESSION})

# Tools that MUTATE the driver's own configuration. They are denied when the task is scoped to one
# application, because changing capture_scope would let the agent widen that grant. Under
# DESKTOP_SESSION the whole disposable GUI session is already granted, so keeping set_config
# hidden only leaves get_desktop_state unusable with the driver's default window scope.
# Keyed by MCP server name, like _WIDENS_OBSERVATION.
_MUTATES_CONFIG: dict[str, tuple[str, ...]] = {
    "desktop-control": ("set_config",),
}


def observation_deny(
    scope: str, servers: tuple[str, ...] | list[str] = ()
) -> list[str]:
    """MCP deny entries that hold Observation inside ``scope``.

    Empty for ``desktop`` scope on purpose: OSWorld's task scope IS the machine, so full-display
    capture and re-scoping are within its grant. Narrowing there would be a different benchmark,
    not better isolation.
    """
    if scope not in SCOPES:
        raise ValueError(f"unknown scope {scope!r}; expected one of {SCOPES}")
    if scope in _MACHINE_IS_THE_SCOPE:
        return []
    out: list[str] = []
    for server in servers:
        for tool in _WIDENS_OBSERVATION.get(server, ()):
            out.append(f"mcp__{server}__{tool}")
    return out


def config_mutation_deny(
    scope: str, servers: tuple[str, ...] | list[str] = ()
) -> list[str]:
    """MCP deny entries for configuration mutation outside a desktop-session grant."""
    if scope not in SCOPES:
        raise ValueError(f"unknown scope {scope!r}; expected one of {SCOPES}")
    if scope in _MACHINE_IS_THE_SCOPE:
        return []
    out: list[str] = []
    for server in servers:
        for tool in _MUTATES_CONFIG.get(server, ()):
            out.append(f"mcp__{server}__{tool}")
    return out


# What a client-side MCP deny can and cannot do, recorded once so no caller has to infer it.
#
# The deny lands in the Claude CLI's own settings.json, which the AGENT process reads. It is not a
# server-side check. Three things follow, all verified rather than assumed:
#   1. The agent holds Bash(*) and the cua-driver executable remains in its PATH. The prompt no
#      longer advertises the direct CLI path, but a client-side MCP deny cannot prevent invoking it.
#   2. On linux/windows the agent's own CLI spawns `cua-driver mcp` (recreation.py:1114), so the
#      process holding the native A11y/UIA rights lives inside the agent's privilege domain.
#   3. Only the Claude path is covered: agent_config.codex_config_toml threads no scope at all.
# Therefore this is a guardrail that narrows the easy path, NOT a boundary. Calling it enforced
# would license a false acceptance later, which is the whole reason the status below is split.
GUARDRAIL = "client_policy_guardrail"
GATEWAY = "gateway_enforced"
UNENFORCED = "unenforced"
UNSUPPORTED = "unsupported"  # no rule or adapter exists for this (scope, backend) pair

# Which backends this module actually has Observation rules for. Absence must report UNSUPPORTED
# rather than inherit a neighbour's status -- the first version derived Observation from the scope
# NAME, so android_app and browser_context reported enforced while generating no denies at all.
_OBSERVATION_BACKENDS: frozenset[str] = frozenset(_WIDENS_OBSERVATION)

# Backends each scope is expected to drive, so "no rule" is distinguishable from "not applicable".
_SCOPE_BACKENDS: dict[str, tuple[str, ...]] = {
    APPLICATION: ("desktop-control",),
    DESKTOP_SESSION: ("desktop-control",),
    ANDROID_APP: ("mobile-mcp",),
    BROWSER_CONTEXT: ("playwright",),
}


def dimensions(scope: str) -> dict[str, dict[str, object]]:
    """Measured status of the four dimensions for ``scope``.

    Status is one of GATEWAY / GUARDRAIL / UNENFORCED / UNSUPPORTED. It is derived from whether a
    rule actually exists for this scope's backends -- never from the scope's name -- and defaults to
    UNSUPPORTED so a backend nobody wrote rules for cannot read as passing.

    ``gap`` states what is still reachable, in the words of the thing that makes it reachable.
    """
    if scope not in SCOPES:
        raise ValueError(f"unknown scope {scope!r}; expected one of {SCOPES}")
    backends = _SCOPE_BACKENDS.get(scope, ())
    have_rules = any(b in _OBSERVATION_BACKENDS for b in backends)
    machine_scoped = scope in _MACHINE_IS_THE_SCOPE

    if machine_scoped:
        observation = {
            "status": UNENFORCED,
            "gap": (
                "the sandbox GUI session IS the task scope, so nothing is narrowed -- by design, "
                "confirmed as the Desktop default. What still needs enforcing is session "
                "MEMBERSHIP (not the Host, not another run, not a second login session or a "
                "Windows secure desktop), which no client-side deny can do."
            ),
        }
    elif not have_rules:
        observation = {
            "status": UNSUPPORTED,
            "gap": (
                f"no Observation rules exist for backends {backends!r}; only "
                f"{sorted(_OBSERVATION_BACKENDS)} are covered"
            ),
        }
    else:
        observation = {
            "status": GUARDRAIL,
            "gap": (
                "client-side only: the deny sits in the agent CLI's settings.json, the prompt "
                "does not prevent invoking the cua-driver executable through Bash, the agent's "
                "own CLI spawns the driver that holds the native rights, and codex is not covered "
                "at all. list_windows and get_window_state also still accept any window."
            ),
        }

    return {
        "target": {
            "status": UNENFORCED,
            "gap": (
                "the prompt hands the agent the reference PID and executable path "
                "(kill_app(pid=...), launch_app(path=...)) and the driver accepts any value. A "
                "Gateway must hold an opaque target handle and refuse agent-supplied PID/HWND."
            ),
        },
        "observation": observation,
        "action": {
            "status": UNENFORCED,
            "gap": (
                "click takes absolute screen coordinates and press_key/hotkey are global; "
                "bounding them to the target window is a driver-side change."
            ),
        },
        "lifetime": {
            # The driver has named sessions with an idle TTL and refuses calls on an expired one
            # ("session 'bmp-recreate' has ended (reason=idle_timeout); tool call 'click' was
            # rejected"). That is a timeout, not a run-scoped capability.
            "status": UNENFORCED,
            "gap": (
                "an idle TTL (CUA_DRIVER_RS_SESSION_IDLE_TTL_SECS), not bound to run_id/phase and "
                "not revoked at seal time -- so it expires on its own schedule, not the run's."
            ),
        },
    }


def gateway_enforced(scope: str) -> bool:
    """True only when every dimension is enforced by something outside the agent's privilege
    domain. Exists so acceptance cannot be claimed off the guardrail."""
    return all(d["status"] == GATEWAY for d in dimensions(scope).values())


__all__ = [
    "config_mutation_deny",
    "APPLICATION",
    "GATEWAY",
    "GUARDRAIL",
    "UNENFORCED",
    "UNSUPPORTED",
    "gateway_enforced",
    "ANDROID_APP",
    "BROWSER_CONTEXT",
    "DESKTOP",
    "DESKTOP_SESSION",
    "SCOPES",
    "dimensions",
    "observation_deny",
]
