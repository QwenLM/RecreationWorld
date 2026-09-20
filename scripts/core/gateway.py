#!/usr/bin/env python3
"""Scoped Gateway: the policy boundary between an untrusted agent and native UI control.

From ``unified-agent-infra.md`` §5. ``core/scope.py`` records WHAT a scope means and measures how
little of it is enforced today; this module is the execution point that can actually enforce it.

WHAT THIS IS, AND WHAT IT IS NOT
--------------------------------
This is the contract and the policy engine: capability minting, opaque target binding, request
authorisation, result filtering, revocation, audit. It is deliberately backend-agnostic and is
**not wired into any platform yet**, so ``scope.gateway_enforced()`` stays False. Shipping the
policy layer before the adapter is the order the reviewer asked for, and it is the order that lets
the negative tests be real: a fake backend can prove a forged PID never reaches native code, which
a source-string test can never prove.

WHY IT CAN BE *INSERTED* RATHER THAN REWRITTEN
---------------------------------------------
cua-driver 0.7.3 already splits into a daemon and a bridge: ``serve`` holds the native
A11y/UIA/AX + input rights, and ``mcp`` proxies to it when a daemon is listening
(``core/cua_driver.py`` documents this: "the ``mcp`` command proxies to the running daemon, and the
coordinate transforms take effect there"). ``CUA_DRIVER_RS_MCP_FORCE_PROXY=1`` makes the bridge
error instead of falling back in-process. So the Gateway is a thin MCP facade in front of that
bridge -- it never reimplements UIA/AT-SPI/AX/capture/input, which is what keeps it clear of the
driver version trilemma (normalised coords only in qwen 0.7.x, the bounded-walk fix only in
trycua >=0.13, macOS pinned to whatever ``QwenCuaDriver.app`` ships).

Measured state of that split, which is why macOS is the first adapter:

    macOS    explicit `serve --socket` + `mcp --socket` + FORCE_PROXY=1   -> fail-closed already
    windows  `autostart kick` starts serve, but the agent's MCP entry has neither FORCE_PROXY
             nor an endpoint                                             -> silent in-process
    linux    no daemon in recreation; spawns `mcp --no-overlay`          -> in-process

THE FOUR RULES, IN THE ORDER THEY ARE CHECKED
---------------------------------------------
1. The capability must exist, belong to this run, be un-revoked, and match the current phase.
   (Lifetime. ``revoke_run`` at seal time is what §3's "封存 Agent" means for a capability.)
2. The tool must be in the scope's action set. (Action.)
3. The request must NOT name its own target. An opaque handle is the entire mechanism: if the agent
   can pass a PID, HWND, bundle id or window title, then narrowing capture scope is decoration --
   the caller simply selects another window. (Target.)
4. Results are filtered to the bound target before they are returned. (Observation.)

Every decision -- allow and deny -- produces an audit record, because §8 requires "每次运行都能产出
可审计的隔离结果" and a denial nobody can see is indistinguishable from a rule nobody wrote.
"""

from __future__ import annotations

import hmac
import os
import uuid
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

from . import scope as scope_mod

# --- denial reasons -------------------------------------------------------------------------
# Constants rather than prose so tests assert on the REASON, not on wording.
NO_SUCH_CAPABILITY = "no_such_capability"
CAPABILITY_REVOKED = "capability_revoked"
WRONG_RUN = "capability_belongs_to_another_run"
WRONG_PHASE = "capability_not_valid_in_this_phase"
TOOL_NOT_IN_SCOPE = "tool_not_in_scope_action_set"
AGENT_SUPPLIED_TARGET = "request_named_its_own_target"
UNKNOWN_TOOL = "unknown_tool"
NO_SUCH_SESSION = "no_such_session"
RUN_SEALED = "run_sealed"
CONFIG_MUTATION_DENIED = "config_mutation_not_granted"
MEMBERSHIP_UNVERIFIABLE = "backend_cannot_attest_session_membership"
TARGET_OUTSIDE_BINDING = "target_outside_bound_session"
SESSION_SELECTOR_DENIED = "request_named_a_session_or_binding_key"

# Parameter names that address a target directly. A request carrying any of these is refused
# outright rather than sanitised: silently dropping a parameter would let the agent believe it
# selected a window and score against whatever it actually got.
TARGET_PARAMS: frozenset[str] = frozenset(
    {
        "pid",
        "process_id",
        "hwnd",
        "window_id",
        "window_handle",
        "window",
        "app_name",
        "app",
        "bundle_id",
        "title",
        "path",
        "executable",
    }
)

# Keys that select a SESSION rather than something inside one: another X display, another sandbox,
# another window station, another host. These are refused for every scope including
# DESKTOP_SESSION -- "the agent may drive its own session" never implies "the agent may say which
# session that is". Found by a mutation test: with these merely injected via setdefault, an agent
# passing display=":0" kept its own value and drove a different display.
SESSION_PARAMS: frozenset[str] = frozenset(
    {
        "display",
        "sandbox_id",
        "session_id",
        "window_station",
        "desktop",
        "host",
        "endpoint",
        "socket",
    }
)

# The recreation prompt's tool catalogue (core/recreation_prompt.py), split by reach.
_APP_TOOLS: frozenset[str] = frozenset(
    {
        "launch_app",
        "bring_to_front",
        "get_window_state",
        "list_windows",
        "kill_app",
        "click",
        "double_click",
        "right_click",
        "type_text",
        "press_key",
        "hotkey",
        "scroll",
        "set_value",
    }
)
# Reach beyond a single app: legitimate when the SESSION is the scope, not under strict-app mode.
_MACHINE_TOOLS: frozenset[str] = frozenset({"set_config", "get_desktop_state"})

# Tools that MUTATE the driver's own configuration. Separate from reach: whoever can call these can
# re-point the backend endpoint, the coordinate space, the session or the audit sink -- i.e. can
# reconfigure the thing enforcing the scope. No scope grants them today.
_CONFIG_TOOLS: frozenset[str] = frozenset({"set_config"})

ALL_TOOLS: frozenset[str] = _APP_TOOLS | _MACHINE_TOOLS

# Tools whose RESULT enumerates or describes windows, so it CAN be filtered to the binding.
_OBSERVATION_TOOLS: frozenset[str] = frozenset({"list_windows", "get_window_state"})


@dataclass(frozen=True)
class ScopePolicy:
    """How one scope answers the four dimensions. A table, not branching, so adding a scope means
    adding a row and a conformance test rather than editing the authorisation path.

    ``config_mutation`` is a SEPARATE axis from capture on purpose. "The agent may photograph the
    whole desktop" and "the agent may re-point the backend endpoint, the audit sink, the session or
    the security policy" are not the same grant, and ``set_config`` is the second one. Allowing it
    because a scope is wide would let the agent reconfigure the thing enforcing the scope.
    """

    tools: frozenset[str]
    accepts_target_selector: bool
    filters_observation: bool
    config_mutation: bool = False
    # Every one of these binding fields must match for something to belong to the binding. A single
    # matching field is not identity: a recycled pid, or a title two windows share, would pass.
    identity_keys: tuple[str, ...] = ()


_POLICIES: dict[str, ScopePolicy] = {
    # One disposable sandbox's GUI session. The agent may enumerate that session's windows, read the
    # whole tree and capture the display, and may name a window inside it -- the boundary is session
    # MEMBERSHIP, which only the backend can attest, so a selector without attestation fails closed.
    scope_mod.DESKTOP_SESSION: ScopePolicy(
        # set_config is IN the tool set deliberately: it is within session reach, so it must be
        # refused by the config-mutation grant rather than by quietly omitting it. Omitting it would
        # leave `config_mutation` dead code -- a rule that never fires and so pins nothing.
        tools=_APP_TOOLS | _MACHINE_TOOLS,
        accepts_target_selector=True,
        filters_observation=False,
        config_mutation=False,
    ),
    # Strict single-app mode: no selector, results narrowed to the bound app. NOT the Desktop
    # default; kept because Android/Web narrowing is this shape, and because making it the desktop
    # default would change what the agent is told it may do and so needs its own A/B.
    scope_mod.APPLICATION: ScopePolicy(
        tools=_APP_TOOLS,
        accepts_target_selector=False,
        filters_observation=True,
        config_mutation=False,
        identity_keys=("pid",),
    ),
}


@dataclass(frozen=True)
class TargetBinding:
    """A target the Trusted Runner registered, addressed by an opaque handle.

    ``address`` is the real thing (a pid, a bundle id, an HWND -- whatever the backend needs) and is
    kept out of ``repr`` so it cannot reach an agent-visible log by accident. The agent receives
    ``handle`` and nothing else; the Gateway substitutes the address on the way to the backend.
    """

    handle: str
    address: dict[str, Any] = field(repr=False, compare=False, default_factory=dict)

    @staticmethod
    def mint(**address: Any) -> "TargetBinding":
        return TargetBinding(
            handle=f"tgt_{uuid.uuid4().hex[:16]}", address=dict(address)
        )

    def for_agent(self) -> dict[str, str]:
        """Everything the agent may know about its target."""
        return {"target": self.handle}


@dataclass
class Capability:
    """Authority to drive one target, for one run, during one phase."""

    token: str
    run_id: str
    phase: str
    scope: str
    target: TargetBinding
    revoked: bool = False

    def for_agent(self) -> dict[str, str]:
        return {"capability": self.token, **self.target.for_agent()}


@dataclass(frozen=True)
class GatewayRequest:
    """Exactly what an agent may put on the wire.

    There is no ``run_id`` and no ``phase`` here, and that is the point: a request that carries its
    own run and phase makes "does this capability belong to the current run/phase" a question the
    caller answers about itself. Both live in server-side session state that the Trusted Runner
    establishes and transitions -- see ``Gateway.open_session`` / ``set_phase`` / ``seal``.
    """

    capability: str
    tool: str
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reason: str = ""
    result: Any = None


@dataclass(frozen=True)
class AuditRecord:
    run_id: str
    phase: str
    tool: str
    allowed: bool
    reason: str
    capability: str
    target: str


class Backend(Protocol):
    """Whatever actually touches the machine. The real one is a cua-driver daemon RPC client
    started with FORCE_PROXY=1 and an explicit endpoint the agent cannot reach.

    A backend MAY also implement::

        def verify_membership(self, params: dict, binding: dict) -> bool

    which answers "is the window/pid this request names inside the bound session?". Only the backend
    can answer it -- the policy layer has no window manager -- so a scope that lets the agent name a
    target REQUIRES this method, and its absence denies such requests rather than trusting them.
    """

    def call(self, tool: str, params: dict[str, Any]) -> Any: ...


@dataclass
class _SessionContext:
    """Server-side truth about a connection: which run it serves and which phase it is in.

    The agent has no way to write any of this. That is the whole reason it exists rather than being
    request fields -- self-reported identity is not identity.
    """

    run_id: str
    phase: str
    sealed: bool = False


class Gateway:
    """Mints capabilities, authorises requests, filters results, revokes.

    Holds no credentials and speaks no platform dialect -- the backend does that. The point of the
    split is that this class can be tested to destruction without a machine to break.
    """

    def __init__(
        self, backend: Backend, audit: Callable[[AuditRecord], None] | None = None
    ) -> None:
        self._backend = backend
        self._caps: dict[str, Capability] = {}
        self._sessions: dict[str, _SessionContext] = {}
        self._audit_sink = audit
        self.audit_log: list[AuditRecord] = []

    # --- lifetime ---------------------------------------------------------------------------

    def open_session(self, *, run_id: str, phase: str) -> str:
        """Trusted Runner opens a connection for one run. Returns the server-side session id."""
        sid = f"sess_{uuid.uuid4().hex[:16]}"
        self._sessions[sid] = _SessionContext(run_id=run_id, phase=phase)
        return sid

    def set_phase(self, session: str, phase: str) -> None:
        """Runner-only phase transition. A sealed session never re-opens -- that would make Seal
        reversible by whoever can name a phase."""
        ctx = self._sessions.get(session)
        if ctx is None:
            raise KeyError(session)
        if ctx.sealed:
            raise RuntimeError("cannot change phase of a sealed session")
        ctx.phase = phase

    def seal(self, run_id: str) -> int:
        """封存 (§3): stop accepting requests for ``run_id``, then revoke its capabilities.

        Ordering is the point. Sealing the sessions FIRST means no request can slip in between the
        two steps; revoking first would leave a window where a session is still live.

        GAP, stated rather than implied: this stops NEW requests. Cancelling calls already in flight
        needs the real transport (a connection to close, an RPC to abort) and belongs to the adapter;
        a dict cannot do it. Eval must not start on ``seal()`` alone until that exists.
        """
        for ctx in self._sessions.values():
            if ctx.run_id == run_id:
                ctx.sealed = True
        return self.revoke_run(run_id)

    def grant(
        self, *, run_id: str, phase: str, scope: str, target: TargetBinding
    ) -> Capability:
        if scope not in scope_mod.SCOPES:
            raise ValueError(
                f"unknown scope {scope!r}; expected one of {scope_mod.SCOPES}"
            )
        cap = Capability(
            token=f"cap_{uuid.uuid4().hex}",
            run_id=run_id,
            phase=phase,
            scope=scope,
            target=target,
        )
        self._caps[cap.token] = cap
        return cap

    def revoke(self, token: str) -> bool:
        cap = self._caps.get(token)
        if cap is None:
            return False
        cap.revoked = True
        return True

    def revoke_run(self, run_id: str) -> int:
        """Seal the agent: every capability minted for ``run_id`` dies here.

        This is the operation §3 calls 封存, and the one an idle TTL is not: a timeout expires on the
        driver's schedule, this expires on the run's.
        """
        n = 0
        for cap in self._caps.values():
            if cap.run_id == run_id and not cap.revoked:
                cap.revoked = True
                n += 1
        return n

    # --- the authorisation path -------------------------------------------------------------

    def handle(self, request: GatewayRequest, *, session: str) -> Decision:
        """Authorise one agent request against server-side session state.

        ``session`` is the connection the Trusted Runner opened -- not something the agent supplies.
        The agent's request names a capability, a tool and params, and nothing about which run or
        phase it thinks it is in.
        """
        ctx = self._sessions.get(session)
        cap = self._caps.get(request.capability)

        def deny(reason: str, target: str = "") -> Decision:
            self._record(ctx, request, False, reason, target)
            return Decision(allowed=False, reason=reason)

        if ctx is None:
            return deny(NO_SUCH_SESSION)
        if ctx.sealed:
            # Checked before the capability so a sealed run says WHY, and so a token that somehow
            # survived revocation still cannot execute.
            return deny(RUN_SEALED)

        # 1. Lifetime
        if cap is None or not hmac.compare_digest(cap.token, request.capability):
            return deny(NO_SUCH_CAPABILITY)
        if cap.revoked:
            return deny(CAPABILITY_REVOKED, cap.target.handle)
        if cap.run_id != ctx.run_id:
            return deny(WRONG_RUN, cap.target.handle)
        if cap.phase != ctx.phase:
            return deny(WRONG_PHASE, cap.target.handle)

        # 2. Action -- from the scope's policy row. A scope with no row denies everything.
        policy = _POLICIES.get(cap.scope)
        if policy is None:
            return deny(TOOL_NOT_IN_SCOPE, cap.target.handle)
        if request.tool not in ALL_TOOLS:
            return deny(UNKNOWN_TOOL, cap.target.handle)
        if request.tool not in policy.tools:
            return deny(TOOL_NOT_IN_SCOPE, cap.target.handle)
        # Config mutation is its own grant, checked separately from reach: a wide observation scope
        # must not imply permission to reconfigure the enforcement point.
        if request.tool in _CONFIG_TOOLS and not policy.config_mutation:
            return deny(CONFIG_MUTATION_DENIED, cap.target.handle)

        # 3. Target, in three steps whose ORDER decides which rule gets reported. Each denial should
        # name the rule actually violated, otherwise the audit log misattributes the refusal.
        #
        # 3a. Naming the session is out of bounds for every scope, strict or wide.
        session_named = sorted(set(request.params) & SESSION_PARAMS)
        if session_named:
            return deny(
                f"{SESSION_SELECTOR_DENIED}:{','.join(session_named)}",
                cap.target.handle,
            )

        # 3b. Strict modes forbid choosing a target at all.
        named = sorted(set(request.params) & TARGET_PARAMS)
        if named and not policy.accepts_target_selector:
            return deny(f"{AGENT_SUPPLIED_TARGET}:{','.join(named)}", cap.target.handle)

        # 3c. No request may name a key the binding owns -- including binding keys a future adapter
        # invents that are not in SESSION_PARAMS. This also makes the injection below unambiguous
        # rather than dependent on who wins a key collision.
        owned = sorted(set(request.params) & set(cap.target.address))
        if owned:
            return deny(
                f"{SESSION_SELECTOR_DENIED}:{','.join(owned)}", cap.target.handle
            )
        if named:
            # The scope permits choosing a target, so the only question left is membership -- and the
            # policy layer cannot answer it, it has no window manager. No attestation => refuse.
            verify = getattr(self._backend, "verify_membership", None)
            if not callable(verify):
                return deny(MEMBERSHIP_UNVERIFIABLE, cap.target.handle)
            try:
                inside = bool(verify(dict(request.params), dict(cap.target.address)))
            except Exception:
                inside = False
            if not inside:
                return deny(
                    f"{TARGET_OUTSIDE_BINDING}:{','.join(named)}", cap.target.handle
                )

        params = dict(request.params)
        # The binding travels with every call so the backend scopes its lookup to the session. Plain
        # assignment is safe because a request naming any binding key was refused above.
        params.update(cap.target.address)
        result = self._backend.call(request.tool, params)

        # 4. Observation -- only where the policy says results are narrowed.
        if policy.filters_observation and request.tool in _OBSERVATION_TOOLS:
            result = self._filter_to_target(result, cap.target, policy)

        self._record(ctx, request, True, "", cap.target.handle)
        return Decision(allowed=True, result=result)

    def _filter_to_target(
        self, result: Any, target: TargetBinding, policy: ScopePolicy
    ) -> Any:
        """Drop anything outside the binding. Strict-app mode only."""
        if isinstance(result, list):
            return [i for i in result if self._belongs(i, target, policy)]
        if isinstance(result, dict) and isinstance(result.get("windows"), list):
            out = dict(result)
            out["windows"] = [
                w for w in result["windows"] if self._belongs(w, target, policy)
            ]
            return out
        return result

    @staticmethod
    def _belongs(item: Any, target: TargetBinding, policy: ScopePolicy) -> bool:
        """ALL of the policy's identity keys must match.

        An any-of match is not identity: a recycled pid, or a title two windows share, would let a
        foreign window through. With no identity keys declared, nothing belongs -- fail closed.
        """
        if not isinstance(item, dict) or not policy.identity_keys:
            return False
        for key in policy.identity_keys:
            if key not in target.address or item.get(key) != target.address[key]:
                return False
        return True

    def _record(
        self,
        ctx: "_SessionContext | None",
        request: GatewayRequest,
        allowed: bool,
        reason: str,
        target: str,
    ) -> None:
        rec = AuditRecord(
            run_id=ctx.run_id if ctx else "",
            phase=ctx.phase if ctx else "",
            tool=request.tool,
            allowed=allowed,
            reason=reason,
            capability=request.capability[:12] if request.capability else "",
            target=target,
        )
        self.audit_log.append(rec)
        if self._audit_sink is not None:
            self._audit_sink(rec)


def backend_env(
    endpoint: str, *, extra: dict[str, str] | None = None
) -> dict[str, str]:
    """Env for the trusted bridge the Gateway starts -- fail-closed, explicit endpoint.

    ``FORCE_PROXY=1`` is the load-bearing one: without it the bridge silently executes in-process
    when the daemon is unreachable, which is precisely the hole that makes today's windows and linux
    wiring not a boundary. With it, a dead daemon is an error instead of a privilege escalation.

    The endpoint is returned for the GATEWAY's child process only. It must never appear in the
    agent's own MCP config; the agent gets the Gateway's frontend and a capability token.
    """
    env = {
        f"{_ENV_PREFIX}MCP_FORCE_PROXY": "1",
        f"{_ENV_PREFIX}SOCKET": endpoint,
        f"{_ENV_PREFIX}UPDATE_CHECK": "0",
    }
    if extra:
        env.update(extra)
    return env


_ENV_PREFIX = "CUA_DRIVER_RS_"


def agent_sees_no_backend(agent_env: dict[str, str], endpoint: str) -> bool:
    """True when ``agent_env`` leaks no route to the native daemon.

    A capability model is void if the token is optional -- an agent that can read the daemon socket
    out of its own environment does not need one.
    """
    if not endpoint:
        return True
    basename = os.path.basename(endpoint)
    for value in agent_env.values():
        if not isinstance(value, str):
            continue
        if endpoint in value or (basename and basename in value):
            return False
    return True


__all__ = [
    "AGENT_SUPPLIED_TARGET",
    "CONFIG_MUTATION_DENIED",
    "MEMBERSHIP_UNVERIFIABLE",
    "NO_SUCH_SESSION",
    "RUN_SEALED",
    "ScopePolicy",
    "SESSION_PARAMS",
    "SESSION_SELECTOR_DENIED",
    "TARGET_OUTSIDE_BINDING",
    "ALL_TOOLS",
    "AuditRecord",
    "Backend",
    "CAPABILITY_REVOKED",
    "Capability",
    "Decision",
    "Gateway",
    "GatewayRequest",
    "NO_SUCH_CAPABILITY",
    "TARGET_PARAMS",
    "TOOL_NOT_IN_SCOPE",
    "TargetBinding",
    "UNKNOWN_TOOL",
    "WRONG_PHASE",
    "WRONG_RUN",
    "agent_sees_no_backend",
    "backend_env",
]
