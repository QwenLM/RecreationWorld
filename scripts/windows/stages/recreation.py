"""Stage: Recreation — agent reverse-engineers reference binary and rebuilds from scratch.

Runs locally on a Windows machine. CUA Driver provides desktop automation
(screenshots, mouse, keyboard, UIA accessibility tree) via MCP stdio transport.

File isolation (rbagent user + ACLs) is always enabled to prevent the agent
from accessing the reference binary. Network isolation (Windows Firewall) is
optional and controlled by RB_WINDOWS_ENABLE_NETWORK_ISOLATION.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from stages.config import (
    CODEX_CONFIG_TOML_TEMPLATE,
    resolve_cua_driver,
    vm_task_dir,
)

# scripts/core is synced to the VM by deploy_and_test.py alongside scripts/windows.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from core import agent_config as core_agent_config  # noqa: E402
from core import agent_invocation as core_agent_invocation  # noqa: E402
from core import cua_driver as core_cua_driver  # noqa: E402
from core import desktop_capture as core_desktop_capture  # noqa: E402
from core import mcp_settings as core_mcp  # noqa: E402
from core import permission_probe as core_permission_probe  # noqa: E402
from core import permission_spec as core_permission_spec  # noqa: E402
from core import recreation_artifact as core_recreation_artifact  # noqa: E402
from core import recreation_paths as core_recreation_paths  # noqa: E402
from core import recreation_prompt as _recreation_prompt  # noqa: E402
from core import runtime_assets  # noqa: E402
from core import scope as core_scope  # noqa: E402
from core import trajectory as core_trajectory  # noqa: E402

# From the ONE shared contract (core/agent_config.py), aligned to linux. windows' own copy
# denied `Agent(*)` but not `Task(*)`, and 2.1.177 keys permissions on Agent while surfacing the
# tool as Task -- so the subagent deny here may never have bitten.
SETTINGS_JSON = core_agent_config.claude_settings(
    mcp_servers=(core_mcp.DESKTOP_SERVER,),
    # The disposable GUI session is the granted scope, matching Linux and macOS.
    scope=core_scope.DESKTOP_SESSION,
)

# ---------------------------------------------------------------------------
# Time-budget forcing-function hooks (Claude only, opt-in via --time-budget-hook)
# ---------------------------------------------------------------------------
# Python interpreter path on the Windows VM (granted RX to rbagent via _TOOL_DIRS).
VM_PYTHON = r"C:\Python312\python.exe"

# SessionStart hook: record the agent's start time once (per session).
_TB_INIT_PY = runtime_assets.load_text("windows/runtime/time_budget_init.py")

# PostToolUse hook: on crossing 25/50/75/90% of the time budget, inject one
# escalating reminder. 25/50 are neutral context; 75/90 are self-conditional
# ("if you have NOT produced the deliverables yet, stop exploring and write code").
_TB_CHECK_PY = runtime_assets.load_text("windows/runtime/time_budget_check.py")


def _write_time_budget_hooks(output_dir: str) -> dict:
    """Write the time-budget hook scripts and return a Claude 'hooks' settings block."""
    init_path = os.path.join(output_dir, "tb_init.py")
    check_path = os.path.join(output_dir, "tb_check.py")
    Path(init_path).write_text(_TB_INIT_PY, encoding="utf-8")
    Path(check_path).write_text(_TB_CHECK_PY, encoding="utf-8")
    py = VM_PYTHON if os.path.isfile(VM_PYTHON) else "python"

    def _cmd(script: str) -> str:
        return f'"{py}" "{script}"'

    return {
        "SessionStart": [{"hooks": [{"type": "command", "command": _cmd(init_path)}]}],
        "PostToolUse": [
            {
                "matcher": "*",
                "hooks": [{"type": "command", "command": _cmd(check_path)}],
            }
        ],
    }


# Build tool directories that the agent user needs read-execute access to
_TOOL_DIRS = [
    r"C:\Program Files\nodejs",
    r"C:\Program Files\dotnet",
    r"C:\Program Files\Git",
    r"C:\Program Files (x86)\Microsoft Visual Studio",
    r"C:\Program Files\Microsoft Visual Studio",
    r"C:\Program Files (x86)\Windows Kits",
    r"C:\ProgramData\chocolatey",
    r"C:\tools",
    r"C:\npm-global",
    r"C:\cargo",
    r"C:\rustup",
    r"C:\Python312",
    str(Path.home() / "AppData" / "Local" / "Programs" / "Python"),
]


from stages.app_lifecycle import (  # noqa: E402
    codex_desktop_mcp_succeeded,
    cua_preflight,
)
from stages.app_lifecycle import (  # noqa: E402
    kill_app as _kill_app,
)
from stages.app_lifecycle import (  # noqa: E402
    launch_app as _launch_app_shared,
)


def _find_exe(build_dir: str) -> str | None:
    """Find the executable path from build_result.json."""
    result_path = os.path.join(build_dir, "build_result.json")
    if not os.path.exists(result_path):
        return None

    result = json.loads(Path(result_path).read_text(encoding="utf-8-sig"))
    if not result.get("success"):
        return None

    exe_path = result.get("executable", "")
    if not exe_path:
        return None
    if os.path.exists(exe_path):
        return exe_path
    if not os.path.isabs(exe_path):
        candidate = os.path.join(build_dir, exe_path)
        if os.path.exists(candidate):
            return candidate
    return None


def _run_ps(script: str, **kwargs) -> subprocess.CompletedProcess:
    """Run a PowerShell snippet."""
    return subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        **kwargs,
    )


def _ps_error_summary(result: subprocess.CompletedProcess) -> str:
    """The one line worth reporting out of a failed PowerShell run.

    ``powershell -Command <script>`` echoes the whole script back inside its error record, so
    passing stderr straight through buried the actual message under twenty lines of the source
    that produced it -- and that blob is what landed in pipeline.log as the only evidence.
    Prefer a line the script wrote deliberately (Write-Error text), then any line that is not
    part of the echoed source or PowerShell's own frame.
    """

    streams = [result.stderr or "", result.stdout or ""]
    # `powershell -Command` renders an error record as the echoed source, then the message on a
    # line of its own introduced by ": ", then its CategoryInfo frame.  That prefix is the only
    # reliable way to tell the message from the `Write-Error (...)` call that produced it.
    for stream in streams:
        for line in stream.splitlines():
            stripped = line.strip()
            if stripped.startswith(":") and stripped[1:].strip():
                return stripped[1:].strip()
    _NOISE = (
        "+",
        "$",
        "@(",
        "}",
        "{",
        "function ",
        "foreach ",
        "if ",
        "return ",
        "Write-",
    )
    for stream in streams:
        for line in stream.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith(_NOISE):
                return stripped
    return "unknown error"


def _long_path(path: str) -> str:
    """Windows extended-length form of ``path``, so a deep tree escapes the 260-char limit.

    An agent's build output nests far deeper than anything the harness authors: one JavaFX
    recreation left a self-nesting ``out/<app>/app/`` chain whose deepest copied file sat at 258
    characters, one 19-character level short of the limit.  shutil.copytree collects per-file
    failures and raises once at the end, so the snapshot looked complete while the whole
    recreation was reported as an infrastructure failure and its score discarded.

    The prefix is used instead of robocopy -- the harness's usual answer to MAX_PATH -- because
    robocopy's symlink and junction handling is not the same as ``copytree(symlinks=True)``, and
    only the length limit is the problem here.  The prefix also disables path normalisation,
    hence the abspath first: a relative or forward-slash path would not resolve.
    """

    if os.name != "nt":
        return path
    return core_recreation_paths.extended_length_path(os.path.abspath(path))


def _remove_workspace_tree(path: str) -> None:
    """Remove one exact workspace path without following a junction or symlink."""

    if not os.path.lexists(path):
        return
    if core_recreation_paths.is_reparse_point(path):
        if os.path.isdir(path):
            os.rmdir(path)
        else:
            os.unlink(path)
    else:
        # Extended-length, because deleting a tree walks the same paths copying it does: the
        # snapshot this removes and re-creates is exactly where MAX_PATH was hit.
        deep = _long_path(path)
        # The handler unlocks only the entry that blocked, so nothing outside the tree is
        # touched. `onexc` replaced `onerror` in 3.12; this package targets >=3.9 and the two
        # take the same three arguments, so one handler serves both.
        if sys.version_info >= (3, 12):
            shutil.rmtree(deep, onexc=core_recreation_paths.force_writable_onerror)
        else:
            shutil.rmtree(deep, onerror=core_recreation_paths.force_writable_onerror)


def _cleanup_snapshotted_tree(path: str, label: str, output_lines: list[str]) -> None:
    """Best-effort removal after the authoritative stage snapshot is durable."""

    try:
        _remove_workspace_tree(path)
    except OSError as exc:
        warning = f"{label}_cleanup_warning={exc}\n"
        print(f"  WARNING: {warning.strip()}")
        output_lines.append(warning)


def _prepare_canonical_workspace(storage_dir: str) -> str:
    """Create the fixed agent path as a real directory, optionally restoring a partial run."""

    canonical = core_recreation_paths.WINDOWS_WORKSPACE_ROOT
    storage = os.path.abspath(storage_dir)
    os.makedirs(os.path.dirname(canonical), exist_ok=True)
    _remove_workspace_tree(canonical)
    os.makedirs(canonical)
    if os.path.isdir(storage):
        shutil.copytree(storage, canonical, dirs_exist_ok=True, symlinks=True)
    if core_recreation_paths.is_reparse_point(canonical):
        raise RuntimeError(f"canonical workspace is not a real directory: {canonical}")
    return canonical


def _snapshot_canonical_workspace(workspace_dir: str, storage_dir: str) -> None:
    """Atomically replace stage storage with the stopped agent's workspace."""

    snapshot = storage_dir + ".snapshot"
    _remove_workspace_tree(snapshot)
    shutil.copytree(_long_path(workspace_dir), _long_path(snapshot), symlinks=True)
    _remove_workspace_tree(storage_dir)
    # Both entry names are short; only the walk underneath them needed the prefix, and
    # os.replace on an extended-length directory is the less well-trodden path.
    os.replace(snapshot, storage_dir)


def _prepare_agent_runtime() -> str:
    """Create a real task-free directory for wrappers, logs, and completion markers."""

    runtime = core_recreation_paths.WINDOWS_RUNTIME_ROOT
    os.makedirs(os.path.dirname(runtime), exist_ok=True)
    _remove_workspace_tree(runtime)
    os.makedirs(runtime)
    if core_recreation_paths.is_reparse_point(runtime):
        raise RuntimeError(f"agent runtime is not a real directory: {runtime}")
    return runtime


def _terminate_agent_processes(user: str = "rbagent") -> None:
    """Stop every process owned by the dedicated agent account and verify the boundary."""

    safe_user = user.replace("'", "''")
    script = runtime_assets.render_text(
        "windows/runtime/terminate_agent_processes.ps1",
        {"__AGENT_USER__": safe_user},
    )
    result = _run_ps(script)
    if result.returncode != 0:
        raise RuntimeError(
            "could not stop all rbagent processes: " + _ps_error_summary(result)
        )


def _kill_workspace_holders(*paths: str) -> str:
    """Best-effort: stop anything still running out of the agent's fixed paths.

    The two kills before this one are scoped differently and both miss the same thing.
    _terminate_agent_processes closes the security boundary by OWNER, and kill_app targets the
    REFERENCE binary by pid and image name -- so a process that is neither, a just-built exe
    relaunched outside rbagent's token or a compiler left behind, keeps its file handle through
    both. The copy and delete below then fail when ``rmtree`` encounters locked
    executables or Git objects.

    Matched by path, not image name, so it needs no guess about what the agent chose to build.
    ``$PID`` is excluded because this script's own command line quotes the paths it searches for.
    Hygiene, not the boundary: failures are returned as text, never raised.
    """

    literals = ", ".join("'" + p.replace("'", "''") + "'" for p in paths if p)
    if not literals:
        return ""
    script = runtime_assets.render_text(
        "windows/runtime/kill_workspace_holders.ps1",
        {"__TARGETS__": literals},
    )
    try:
        result = _run_ps(script, timeout=60)
    except Exception as exc:  # noqa: BLE001 - a failed sweep must not fail a scored run
        return f"sweep failed: {exc}"
    return (result.stdout or "").strip()


def _lock_agent_tree(path: str, user: str = "rbagent") -> None:
    """Transfer a frozen tree to this process and remove every agent ACL.

    ``USERNAME`` is not guaranteed to be populated for the SSH/scheduled-task
    process that runs the Windows worker.  Falling back to ``Administrator``
    can therefore remove inheritance and lock the worker out of its own files.
    Resolve the effective token instead, and grant it explicitly *before*
    inheritance is removed.
    """

    identity = _run_ps("[System.Security.Principal.WindowsIdentity]::GetCurrent().Name")
    trusted = identity.stdout.strip()
    if identity.returncode != 0 or not trusted:
        raise RuntimeError(
            "could not resolve the trusted Windows identity: "
            + (identity.stderr.strip() or identity.stdout.strip() or "unknown error")
        )
    commands = (
        ["icacls", path, "/setowner", trusted, "/T", "/C", "/Q"],
        ["icacls", path, "/grant:r", f"{trusted}:(OI)(CI)F", "/T", "/C", "/Q"],
        # Keep service/admin recovery access independent of localized account names.
        ["icacls", path, "/grant", "*S-1-5-18:(OI)(CI)F", "/T", "/C", "/Q"],
        ["icacls", path, "/grant", "*S-1-5-32-544:(OI)(CI)F", "/T", "/C", "/Q"],
        # Disable inheritance while copying inherited ACEs to explicit entries.  Using
        # ``/inheritance:r`` recursively also removes the trusted/SYSTEM grants that
        # children inherited from this root, leaving files unreadable by the eval stage.
        # The following removals still strip every explicit agent ACE.
        ["icacls", path, "/inheritance:d", "/T", "/C", "/Q"],
        ["icacls", path, "/remove:g", user, "/T", "/C", "/Q"],
        ["icacls", path, "/remove:d", user, "/T", "/C", "/Q"],
    )
    for command in commands:
        result = subprocess.run(command, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"could not lock agent tree {path!r}: "
                + (
                    result.stderr.strip()
                    or result.stdout.strip()
                    or str(result.returncode)
                )
            )


# ---------------------------------------------------------------------------
# Network isolation — Windows Firewall
# ---------------------------------------------------------------------------


_FIREWALL_PROFILES = ("Domain", "Private", "Public")


def _checked_command(command: list[str], description: str) -> subprocess.CompletedProcess:
    """Run a firewall command and fail with usable evidence instead of claiming success."""
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip()
        raise RuntimeError(
            f"{description} failed (exit={result.returncode}): {detail or 'no output'}"
        )
    return result


def _checked_ps(script: str, description: str) -> subprocess.CompletedProcess:
    result = _run_ps("$ErrorActionPreference = 'Stop'; " + script)
    if result.returncode != 0:
        raise RuntimeError(f"{description} failed: {_ps_error_summary(result)}")
    return result


def _ps_literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _firewall_profiles() -> list[dict[str, str]]:
    """Return the effective state that must be identical after restoration."""
    result = _checked_ps(
        "Get-NetFirewallProfile -PolicyStore ActiveStore | Sort-Object Name | "
        "ForEach-Object { [pscustomobject]@{ "
        "Name = [string]$_.Name; Enabled = [string]$_.Enabled; "
        "DefaultInboundAction = [string]$_.DefaultInboundAction; "
        "DefaultOutboundAction = [string]$_.DefaultOutboundAction } } | "
        "ConvertTo-Json -Compress",
        "query effective Windows Firewall profiles",
    )
    try:
        decoded = json.loads(result.stdout.lstrip("\ufeff").strip())
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("Windows Firewall profile query returned invalid JSON") from exc
    profiles = [decoded] if isinstance(decoded, dict) else list(decoded or [])
    expected = {name.lower() for name in _FIREWALL_PROFILES}
    found = {str(item.get("Name", "")).lower() for item in profiles}
    if found != expected:
        raise RuntimeError(
            f"Windows Firewall profile query returned {sorted(found)}, expected {sorted(expected)}"
        )
    return sorted(profiles, key=lambda item: str(item["Name"]).lower())


def _resolve_endpoint(endpoint: str) -> dict[str, object]:
    parsed = urlparse(endpoint)
    host = parsed.hostname or ""
    if not host:
        raise RuntimeError(f"network isolation endpoint has no hostname: {endpoint!r}")
    if parsed.scheme not in ("http", "https"):
        raise RuntimeError(
            f"network isolation endpoint must use http or https, got {parsed.scheme!r}"
        )
    port = int(parsed.port or (443 if parsed.scheme == "https" else 80))
    addresses: set[str] = set()
    ip_literal = False
    try:
        addresses.add(str(ipaddress.ip_address(host)))
        ip_literal = True
    except ValueError:
        try:
            for family, _, _, _, sockaddr in socket.getaddrinfo(
                host, port, type=socket.SOCK_STREAM
            ):
                if family in (socket.AF_INET, socket.AF_INET6):
                    addresses.add(str(ipaddress.ip_address(sockaddr[0])))
        except OSError as exc:
            raise RuntimeError(f"could not resolve network endpoint {host!r}: {exc}") from exc
    if not addresses:
        raise RuntimeError(f"network endpoint {host!r} resolved to no IP addresses")
    ordered = sorted(addresses, key=lambda value: (ipaddress.ip_address(value).version, value))
    return {
        "scheme": parsed.scheme,
        "host": host,
        "port": port,
        "addresses": ordered,
        "ip_literal": ip_literal,
    }


def _dns_servers() -> list[str]:
    result = _checked_ps(
        "@(Get-DnsClientServerAddress | ForEach-Object { $_.ServerAddresses } | "
        "Where-Object { $_ } | Sort-Object -Unique) | ConvertTo-Json -Compress",
        "query configured DNS servers",
    )
    try:
        decoded = json.loads(result.stdout.lstrip("\ufeff").strip() or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError("DNS server query returned invalid JSON") from exc
    values = [decoded] if isinstance(decoded, str) else list(decoded or [])
    addresses = []
    for value in values:
        try:
            address = str(ipaddress.ip_address(str(value).split("%", 1)[0]))
        except ValueError:
            continue
        if address not in ("0.0.0.0", "::"):
            addresses.append(address)
    return sorted(set(addresses))


def _tcp_probe(addresses: list[str], port: int, timeout: float = 3.0) -> tuple[bool, str]:
    errors = []
    for address in addresses:
        try:
            with socket.create_connection((address, int(port)), timeout=timeout):
                return True, f"{address}:{port}"
        except OSError as exc:
            errors.append(f"{address}:{port} ({type(exc).__name__}: {exc})")
    return False, "; ".join(errors) or "no addresses"


def _write_network_report(state: dict[str, object]) -> None:
    path = Path(str(state["report_path"]))
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def _firewall_rules(rule_names: list[str]) -> list[dict[str, str]]:
    if not rule_names:
        return []
    names = ",".join(_ps_literal(name) for name in rule_names)
    result = _checked_ps(
        f"$names = @({names}); @($names | ForEach-Object {{ "
        "$rule = Get-NetFirewallRule -DisplayName $_ -ErrorAction SilentlyContinue; "
        "if ($rule) { $rule | ForEach-Object { [pscustomobject]@{ "
        "DisplayName = [string]$_.DisplayName; Enabled = [string]$_.Enabled; "
        "Direction = [string]$_.Direction; Action = [string]$_.Action } } } }) | "
        "ConvertTo-Json -Compress",
        "query RecreationBench firewall rules",
    )
    try:
        decoded = json.loads(result.stdout.lstrip("\ufeff").strip() or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError("Windows Firewall rule query returned invalid JSON") from exc
    return [decoded] if isinstance(decoded, dict) else list(decoded or [])


def _restore_firewall_profiles(profiles: list[dict[str, str]]) -> None:
    for profile in profiles:
        name = str(profile["Name"])
        if name not in _FIREWALL_PROFILES:
            raise RuntimeError(f"refusing to restore unexpected firewall profile {name!r}")
        _checked_ps(
            "Set-NetFirewallProfile "
            f"-Profile {_ps_literal(name)} "
            f"-Enabled {_ps_literal(str(profile['Enabled']))} "
            f"-DefaultInboundAction {_ps_literal(str(profile['DefaultInboundAction']))} "
            f"-DefaultOutboundAction {_ps_literal(str(profile['DefaultOutboundAction']))}",
            f"restore Windows Firewall profile {name}",
        )


def _restore_network(state: dict[str, object]) -> None:
    """Restore the exact exported policy and prove that the prior profiles are back."""
    backup_path = str(state["backup_path"])
    state["restore_started_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    try:
        _checked_command(
            ["netsh", "advfirewall", "import", backup_path],
            "restore Windows Firewall policy backup",
        )
        restore_method = "netsh_import"
    except RuntimeError as import_error:
        # The setup only changes profile defaults and adds uniquely named rules. This fallback
        # still restores those exact deltas if netsh cannot import the full backup.
        fallback_errors = []
        for name in state.get("rule_names", []):
            try:
                _checked_command(
                    [
                        "netsh",
                        "advfirewall",
                        "firewall",
                        "delete",
                        "rule",
                        f"name={name}",
                    ],
                    f"remove temporary firewall rule {name}",
                )
            except RuntimeError as exc:
                fallback_errors.append(str(exc))
        try:
            _restore_firewall_profiles(list(state["profiles_before"]))
        except RuntimeError as exc:
            fallback_errors.append(str(exc))
        if fallback_errors:
            state["restore_error"] = str(import_error) + "; " + "; ".join(fallback_errors)
            try:
                _write_network_report(state)
            except OSError:
                pass
            raise RuntimeError(str(state["restore_error"])) from import_error
        restore_method = "profile_and_rule_fallback"
        state["restore_import_error"] = str(import_error)

    profiles_after = _firewall_profiles()
    rules_after = _firewall_rules(list(state.get("rule_names", [])))
    if profiles_after != state["profiles_before"] or rules_after:
        state["restore_error"] = (
            "firewall restoration verification failed: "
            f"profiles_match={profiles_after == state['profiles_before']} "
            f"temporary_rules_remaining={len(rules_after)}"
        )
        state["profiles_after_restore"] = profiles_after
        state["temporary_rules_after_restore"] = rules_after
        try:
            _write_network_report(state)
        except OSError:
            pass
        raise RuntimeError(str(state["restore_error"]))

    state["restored"] = True
    state["restore_method"] = restore_method
    state["restored_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    state["profiles_after_restore"] = profiles_after
    _write_network_report(state)
    print("  Network isolation restored and verified")


def _check_previous_network_isolation() -> None:
    """Refuse to treat a previous run's allow rules as the host's clean baseline."""
    result = _checked_ps(
        "Get-NetFirewallRule -PolicyStore ActiveStore | Where-Object { "
        "$_.Enabled -eq 'True' -and $_.Direction -eq 'Outbound' -and "
        "$_.Action -eq 'Allow' -and $_.DisplayName -match "
        "'^RB_(Allow_(Proxy|Localhost|LocalSubnet|DNS)|"
        "[0-9a-f]{12}_Allow_(Endpoint|DNS_(TCP|UDP))_IPv[46])$' "
        "} | Select-Object -ExpandProperty DisplayName | ConvertTo-Json -Compress",
        "check for leftover RecreationBench firewall rules",
    )
    try:
        decoded = json.loads(result.stdout.lstrip("\ufeff").strip() or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError("leftover firewall rule query returned invalid JSON") from exc
    names = [decoded] if isinstance(decoded, str) else decoded
    if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
        raise RuntimeError("leftover firewall rule query returned invalid rule names")
    if names:
        raise RuntimeError(
            f"previous RecreationBench network isolation is still active: {sorted(names)}; "
            "finish the active run or restore the host firewall policy before retrying"
        )


def _setup_network_isolation(endpoint: str, output_dir: str) -> dict[str, object]:
    """Apply an audited, reversible outbound firewall boundary."""
    _check_previous_network_isolation()
    allowed = _resolve_endpoint(endpoint)
    denied_candidates = []
    for candidate in (
        os.environ.get("RB_WINDOWS_NETWORK_DENY_PROBE", "https://1.1.1.1:443"),
    ):
        if not candidate:
            continue
        try:
            resolved = _resolve_endpoint(candidate)
        except RuntimeError:
            continue
        resolved["addresses"] = [
            address
            for address in resolved["addresses"]
            if address not in allowed["addresses"]
        ]
        if resolved["addresses"]:
            denied_candidates.append(resolved)

    denied_probe = None
    for candidate in denied_candidates:
        reachable, detail = _tcp_probe(
            list(candidate["addresses"]), int(candidate["port"]), timeout=2.0
        )
        if reachable:
            denied_probe = {
                "host": candidate["host"],
                "port": candidate["port"],
                "addresses": candidate["addresses"],
                "pre_isolation": detail,
            }
            break

    dns_servers: list[str] = []
    if not bool(allowed["ip_literal"]) and str(allowed["host"]).lower() != "localhost":
        dns_servers = _dns_servers()
        if not dns_servers:
            raise RuntimeError("network endpoint uses a hostname but no DNS servers were found")

    run_id = uuid.uuid4().hex[:12]
    backup_path = os.path.join(output_dir, f"network_firewall_before_{run_id}.wfw")
    report_path = os.path.join(output_dir, "network_isolation_report.json")
    profiles_before = _firewall_profiles()
    state: dict[str, object] = {
        "schema_version": 1,
        "run_id": run_id,
        "backup_path": backup_path,
        "report_path": report_path,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "profiles_before": profiles_before,
        "allowed_endpoint": allowed,
        "denied_probe": denied_probe,
        "rule_names": [],
        "setup_verified": False,
        "restored": False,
    }
    if dns_servers:
        state["dns_servers"] = dns_servers

    _checked_command(
        ["netsh", "advfirewall", "export", backup_path],
        "backup Windows Firewall policy",
    )
    if not os.path.isfile(backup_path) or os.path.getsize(backup_path) == 0:
        raise RuntimeError(
            f"Windows Firewall export reported success but produced no backup: {backup_path}"
        )
    _write_network_report(state)

    rule_prefix = f"RB_{run_id}"
    rule_specs: list[tuple[str, list[str]]] = []

    def add_address_rules(kind: str, addresses: list[str], params: list[str]) -> None:
        # netsh rejects a comma-separated remoteip value that mixes IPv4 and IPv6, even though
        # both families are individually valid. Keep each family in its own auditable rule.
        for version in (4, 6):
            family_addresses = [
                address
                for address in addresses
                if ipaddress.ip_address(address).version == version
            ]
            if family_addresses:
                rule_specs.append(
                    (
                        f"{rule_prefix}_{kind}_IPv{version}",
                        [f"remoteip={','.join(family_addresses)}", *params],
                    )
                )

    add_address_rules(
        "Allow_Endpoint",
        list(allowed["addresses"]),
        [f"remoteport={allowed['port']}", "protocol=tcp"],
    )
    if dns_servers:
        for protocol in ("udp", "tcp"):
            add_address_rules(
                f"Allow_DNS_{protocol.upper()}",
                dns_servers,
                [
                    "remoteport=53",
                    f"protocol={protocol}",
                ],
            )
    state["rule_names"] = [name for name, _ in rule_specs]
    _write_network_report(state)
    try:
        # Install exceptions before changing the default so the model endpoint is never cut off
        # in the middle of setup.
        for name, params in rule_specs:
            _checked_command(
                [
                    "netsh",
                    "advfirewall",
                    "firewall",
                    "add",
                    "rule",
                    f"name={name}",
                    "dir=out",
                    "action=allow",
                    "profile=any",
                    "enable=yes",
                    *params,
                ],
                f"create temporary firewall rule {name}",
            )
        _checked_ps(
            "Set-NetFirewallProfile -Profile Domain,Private,Public "
            "-Enabled True -DefaultOutboundAction Block",
            "enable outbound-blocking Windows Firewall policy",
        )

        profiles_active = _firewall_profiles()
        profiles_before_by_name = {
            str(profile["Name"]): profile for profile in profiles_before
        }
        bad_profiles = [
            profile["Name"]
            for profile in profiles_active
            if str(profile["Enabled"]).lower() not in ("true", "1")
            or str(profile["DefaultInboundAction"]).lower()
            != str(
                profiles_before_by_name[str(profile["Name"])]["DefaultInboundAction"]
            ).lower()
            or str(profile["DefaultOutboundAction"]).lower() not in ("block", "2")
        ]
        rules_active = _firewall_rules(list(state["rule_names"]))
        good_rule_names = {
            str(rule["DisplayName"])
            for rule in rules_active
            if str(rule["Enabled"]).lower() in ("true", "1")
            and str(rule["Direction"]).lower() in ("outbound", "2")
            and str(rule["Action"]).lower() in ("allow", "1")
        }
        missing_rules = sorted(set(state["rule_names"]) - good_rule_names)
        if bad_profiles or missing_rules:
            raise RuntimeError(
                "firewall policy attestation failed: "
                f"bad_profiles={bad_profiles}, missing_or_invalid_rules={missing_rules}"
            )

        allowed_ok, allowed_detail = _tcp_probe(
            list(allowed["addresses"]), int(allowed["port"]), timeout=5.0
        )
        if not allowed_ok:
            raise RuntimeError(
                f"allowed endpoint is unreachable after isolation: {allowed_detail}"
            )
        denied_result: dict[str, object] = {
            "performed": False,
            "passed": None,
            "detail": "pre-isolation target unavailable; policy-only verification",
        }
        if denied_probe:
            denied_ok, denied_detail = _tcp_probe(
                list(denied_probe["addresses"]), int(denied_probe["port"]), timeout=2.0
            )
            if denied_ok:
                raise RuntimeError(
                    f"forbidden direct endpoint remained reachable after isolation: {denied_detail}"
                )
            denied_result = {
                "performed": True,
                "passed": True,
                "detail": denied_detail,
            }

        state["profiles_during_isolation"] = profiles_active
        state["rules_during_isolation"] = rules_active
        state["allowed_probe"] = {"passed": True, "detail": allowed_detail}
        state["denied_probe_result"] = denied_result
        state["setup_verified"] = True
        state["verified_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        _write_network_report(state)
    except BaseException as setup_error:
        state["setup_error"] = f"{type(setup_error).__name__}: {setup_error}"
        try:
            _restore_network(state)
        except RuntimeError as restore_error:
            raise RuntimeError(
                f"network isolation setup failed ({setup_error}); rollback also failed "
                f"({restore_error}); policy backup retained at {backup_path}"
            ) from setup_error
        raise

    print(
        "  Network isolation enabled and verified "
        f"(endpoint={allowed['host']}:{allowed['port']}, rules={len(rule_specs)})"
    )
    return state


# ---------------------------------------------------------------------------
# File isolation — rbagent user + ACLs
# ---------------------------------------------------------------------------


def _setup_file_isolation(
    task_base_dir: str,
    output_dir: str,
    workspace_dir: str,
    runtime_dir: str,
    mcp_config_path: str,
) -> str:
    """Apply and attest the shared boundary, then retain Windows-only tool grants.

    The shared probe creates/resets ``rbagent``, owns the ACL contract, verifies access through the
    effective token and leaves the generated password in ``RB_AGENT_PASSWORD`` for the launch below.
    """
    # Protect the storage root itself, not merely the current task's children. Otherwise the agent
    # can list C:\rb_pipeline, learn the stable task hash, and dictionary-match it back to an app.
    protected = [os.path.dirname(os.path.abspath(task_base_dir))]
    pipeline_dir = r"C:\RecreationBench"
    if os.path.exists(pipeline_dir):
        protected.append(pipeline_dir)
    if not protected:
        raise RuntimeError("permission setup found no protected Windows task paths")

    permission_work = os.path.join(output_dir, ".permission_setup")
    ok, transcript = core_permission_probe.run_local(
        "windows",
        os.path.basename(task_base_dir),
        protected,
        [workspace_dir, runtime_dir],
        work=permission_work,
        user="rbagent",
    )
    print(transcript or "(no output from unified permission setup)")
    if not ok:
        raise RuntimeError("unified permission setup did not attest")
    report = os.path.join(permission_work, "report.json")
    if not os.path.isfile(report):
        raise RuntimeError("unified permission setup produced no report")
    shutil.copyfile(report, os.path.join(output_dir, "permission_report.json"))

    isolation_password = os.environ.get(core_permission_spec.WINDOWS_PASSWORD_ENV, "")
    if not isolation_password:
        raise RuntimeError(
            "unified permission setup produced no Windows agent credential"
        )

    # Tool and MCP grants are runtime capabilities, not part of the protected/writable boundary.
    if os.path.isfile(mcp_config_path):
        grant = subprocess.run(
            ["icacls", mcp_config_path, "/grant", "rbagent:(R)", "/Q"],
            capture_output=True,
        )
        if grant.returncode != 0:
            raise RuntimeError(
                f"could not grant rbagent read access to MCP config: rc={grant.returncode}"
            )

    tool_dirs = list(_TOOL_DIRS) + [
        os.path.join(os.environ.get("APPDATA", ""), "npm"),
        os.path.join(os.environ.get("USERPROFILE", ""), ".dotnet"),
    ]
    for d in tool_dirs:
        if os.path.isdir(d):
            subprocess.run(
                ["icacls", d, "/grant", "rbagent:(OI)(CI)(RX)", "/T", "/Q"],
                capture_output=True,
            )

    print("  Unified file isolation configured and attested")
    return isolation_password


def _autogen_build_ps1(rec_output: str) -> bool:
    """Auto-generate build.ps1 when agent produced source but no build script."""
    src_dir = os.path.join(rec_output, "src")
    if not os.path.isdir(src_dir):
        return False

    build_path = os.path.join(rec_output, "build.ps1")
    bin_dir = os.path.join(rec_output, "bin")

    csproj = list(Path(src_dir).rglob("*.csproj"))
    if csproj:
        proj = csproj[0]
        script = runtime_assets.render_text(
            "windows/runtime/autobuild_csproj.ps1",
            {
                "__PROJECT__": str(proj.relative_to(src_dir)).replace("'", "''"),
            },
        )
        Path(build_path).write_text(script, encoding="utf-8")
        os.makedirs(bin_dir, exist_ok=True)
        print(f"  Auto-generated build.ps1 for .csproj: {proj.name}")
        return True

    sln = list(Path(src_dir).rglob("*.sln"))
    if sln:
        script = runtime_assets.render_text(
            "windows/runtime/autobuild_solution.ps1",
            {
                "__SOLUTION__": str(sln[0].relative_to(src_dir)).replace("'", "''"),
            },
        )
        Path(build_path).write_text(script, encoding="utf-8")
        os.makedirs(bin_dir, exist_ok=True)
        print(f"  Auto-generated build.ps1 for .sln: {sln[0].name}")
        return True

    pkg_json = list(Path(src_dir).rglob("package.json"))
    if pkg_json:
        pkg_dir = pkg_json[0].parent.relative_to(src_dir)
        script = runtime_assets.render_text(
            "windows/runtime/autobuild_npm.ps1",
            {
                "__PACKAGE_DIR__": str(pkg_dir).replace("'", "''"),
            },
        )
        Path(build_path).write_text(script, encoding="utf-8")
        os.makedirs(bin_dir, exist_ok=True)
        print("  Auto-generated build.ps1 for package.json")
        return True

    return False


def _build_claude_wrapper(
    *,
    output_dir,
    workspace_dir,
    client_model,
    effort,
    timeout,
    base_url,
    mcp_config_path,
    auto_compact_window,
    autocompact_pct,
    claude_code_max_output_tokens,
    extra_body,
    prompt_path,
    trajectory_path,
    stderr_path,
    debug_log,
    env,
    cua_driver_path,
    cua_env,
    ref_app_pid,
    cua_preflight_mode,
    time_budget_hook_sec=0,
) -> str:
    """Generate the Windows process/session wrapper around the shared invocation."""
    claude_path = shutil.which("claude", path=env.get("PATH"))
    if claude_path and claude_path.lower().endswith(".cmd"):
        exe_candidate = os.path.join(
            os.path.dirname(claude_path),
            "node_modules",
            "@anthropic-ai",
            "claude-code",
            "bin",
            "claude.exe",
        )
        if os.path.isfile(exe_candidate):
            claude_path = exe_candidate
    if not claude_path:
        claude_path = "claude"

    spec = core_agent_invocation.InvocationSpec(
        agent_cli="claude",
        # The Claude Code CLIENT model, not the upstream route.  Everything this wrapper does with
        # a model name is client-side -- the spec below becomes ANTHROPIC_MODEL and its three
        # per-tier aliases -- and Claude Code reads its context window, its token accounting and
        # its beta headers off that name.  Passing the upstream id here made a kimi-k3 arm look
        # like an unknown model, so the CLI fell back to its default 200K window: with
        # CLAUDE_CODE_AUTO_COMPACT_WINDOW=1048576 set and honoured, auto-compaction still fired at
        # ~165K.  16 apps in one batch spent 18.4 hours across 292 compactions that way, and three
        # died on "Prompt is too long" inside what was supposed to be a 1M window.
        #
        # The decoration is decided once, on the pod, by vm_runtime's claude_client_model() from
        # RB_CONTEXT_1M, and arrives here through --claude-model.  Do not recompute it: this value
        # is already normalized, and running claude_client_model() over it again would strip the
        # [1m] it carries.
        model=client_model,
        prompt_file=prompt_path,
        workspace=workspace_dir,
        trajectory_path=trajectory_path,
        stderr_path=stderr_path,
        timeout_sec=timeout,
        request_timeout_ms=int(env["API_TIMEOUT_MS"]),
        tool_timeout_ms=int(env[core_mcp.TOOL_TIMEOUT_VAR]),
        mcp_config=mcp_config_path,
        reasoning_effort=effort,
        run_name="rb-recreation",
        executable=claude_path,
        capture_tool_use_screenshots=os.environ.get(
            "RB_CAPTURE_TOOL_USE_SCREENSHOTS", "false"
        )
        .strip()
        .lower()
        in ("1", "true", "yes", "on"),
        screenshot_dir=os.path.join(output_dir, "tool_use_screenshots"),
        screenshot_command=(
            VM_PYTHON,
            os.path.join(output_dir, "desktop_capture.py"),
            "{output}",
        ),
    )
    runner_path = os.path.join(output_dir, "agent_invocation.py")
    spec_path = os.path.join(output_dir, "invocation.json")
    shutil.copyfile(Path(core_agent_invocation.__file__), runner_path)
    shutil.copyfile(
        Path(core_agent_invocation.__file__).with_name("tool_use_capture.py"),
        os.path.join(output_dir, "tool_use_capture.py"),
    )
    shutil.copyfile(
        Path(core_agent_invocation.__file__).with_name("desktop_capture.py"),
        os.path.join(output_dir, "desktop_capture.py"),
    )
    core_agent_invocation.write_spec(spec_path, spec)
    shared_env = core_agent_invocation.runtime_env(spec, {})

    extra_env_lines = ""
    # Third-party Anthropic-compatible gateways may reject beta headers added by
    # newer Claude Code releases (for example prompt-caching-scope-2026-01-05).
    # The benchmark does not rely on those experimental protocol features.
    extra_env_lines += "$env:CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS = '1'\n"
    if os.environ.get("RB_WINDOWS_ENABLE_NETWORK_ISOLATION", "1").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    ):
        # Optional telemetry and updater endpoints are intentionally outside the model endpoint
        # allow rule. Avoid spending the recreation budget retrying traffic that must be blocked.
        extra_env_lines += "$env:CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC = '1'\n"
    if auto_compact_window:
        extra_env_lines += (
            f"$env:CLAUDE_CODE_AUTO_COMPACT_WINDOW = '{auto_compact_window}'\n"
        )
    if autocompact_pct:
        extra_env_lines += (
            f"$env:CLAUDE_AUTOCOMPACT_PCT_OVERRIDE = '{autocompact_pct}'\n"
        )
    if claude_code_max_output_tokens:
        extra_env_lines += (
            f"$env:CLAUDE_CODE_MAX_OUTPUT_TOKENS = '{claude_code_max_output_tokens}'\n"
        )
    if time_budget_hook_sec:
        extra_env_lines += f"$env:RB_TIME_BUDGET_SEC = '{time_budget_hook_sec}'\n"
        extra_env_lines += f"$env:RB_TIME_HOOK_STATE_DIR = '{output_dir}'\n"
    if extra_body:
        eb_b64 = base64.b64encode(extra_body.encode("utf-8")).decode("ascii")
        extra_env_lines += (
            f"$env:CLAUDE_CODE_EXTRA_BODY = "
            f"(New-Object System.Text.UTF8Encoding($false)).GetString("
            f"[Convert]::FromBase64String('{eb_b64}'))\n"
            f'"$(Get-Date -Format o) CLAUDE_CODE_EXTRA_BODY=$($env:CLAUDE_CODE_EXTRA_BODY)" | Out-File $dbg -Append -Encoding utf8\n'
        )

    mcp_probe_path = os.path.join(output_dir, "mcp_probe.py")
    agent_exit_path = os.path.join(output_dir, "agent_exit_code.txt")
    shutil.copyfile(
        Path(__file__).resolve().parents[2] / "core" / "mcp_probe.py",
        mcp_probe_path,
    )
    mcp_env_ps1 = "\n".join(
        f"$env:{key} = '{str(value).replace(chr(39), chr(39) * 2)}'"
        for key, value in cua_env.items()
    )

    wrapper_content = runtime_assets.render_text(
        "windows/runtime/claude_wrapper.ps1",
        {
            "__DEBUG_LOG__": debug_log,
            "__AGENT_EXIT_PATH__": agent_exit_path,
            "__WORKSPACE_DIR__": workspace_dir,
            "__BASE_URL__": base_url,
            "__CLIENT_MODEL__": client_model,
            "__CLAUDE_CODE_MAX_RETRIES__": shared_env["CLAUDE_CODE_MAX_RETRIES"],
            "__MAX_STRUCTURED_OUTPUT_RETRIES__": shared_env[
                "MAX_STRUCTURED_OUTPUT_RETRIES"
            ],
            "__API_TIMEOUT_MS__": shared_env["API_TIMEOUT_MS"],
            "__MCP_TOOL_TIMEOUT_EXPORT__": core_mcp.tool_timeout_export_ps1(
                shared_env["MCP_TOOL_TIMEOUT"]
            ),
            "__MCP_ENV__": mcp_env_ps1,
            "__EXTRA_ENV__": extra_env_lines,
            "__PROMPT_PATH__": prompt_path,
            "__CUA_PREFLIGHT_MODE__": cua_preflight_mode,
            "__REFERENCE_PID__": int(ref_app_pid),
            "__VM_PYTHON__": VM_PYTHON,
            "__MCP_PROBE_PATH__": mcp_probe_path,
            "__CUA_DRIVER_PATH__": cua_driver_path,
            "__CUA_DAEMON_PIPE__": core_cua_driver.WINDOWS_DAEMON_PIPE,
            "__RUNNER_PATH__": runner_path,
            "__SPEC_PATH__": spec_path,
            "__TRAJECTORY_PATH__": trajectory_path,
            "__STDERR_PATH__": stderr_path,
        },
    )
    wrapper_path = os.path.join(output_dir, "run_claude.ps1")
    Path(wrapper_path).write_text(wrapper_content, encoding="utf-8")
    return wrapper_path


def _build_codex_wrapper(
    *,
    output_dir,
    workspace_dir,
    model,
    effort,
    timeout,
    codex_base_url,
    codex_mcp,
    cua_driver_path,
    cua_env,
    ref_app_pid,
    prompt_path,
    trajectory_path,
    stderr_path,
    debug_log,
    env,
    mcp_tool_timeout_ms,
    request_timeout_ms,
    cua_preflight_mode,
) -> str:
    """Generate the Windows process/session wrapper around the shared invocation."""
    # Build config.toml content.
    # The BARE slug, via the shared rule (core/agent_config.codex_model_slug) -- linux has always
    # done this and windows never did, because windows' codex path had no way to run. The first
    # real windows codex run failed on exactly the two symptoms that rule exists for:
    #   "Model metadata for `openai.gpt-5.6-sol` not found. Defaulting to fallback metadata"
    #   "/responses: Invalid model name passed in model=openai.gpt-5.6-sol"
    # -- both the SIDECAR upstream name and litellm alias must use the bare slug.
    codex_config = CODEX_CONFIG_TOML_TEMPLATE.format(
        model=core_agent_config.codex_model_slug(model),
        base_url=codex_base_url,
    )
    codex_effort = effort.lower()
    eff_lines = (
        f'model_reasoning_effort = "{codex_effort}"\n'
        'model_reasoning_summary = "auto"\n'
        "model_supports_reasoning_summaries = true\n"
    )
    ins = codex_config.find("[model_providers")
    if ins >= 0:
        codex_config = codex_config[:ins] + eff_lines + "\n" + codex_config[ins:]
    else:
        codex_config += eff_lines
    windows_mcp_args = [
        "mcp",
        "--socket",
        core_cua_driver.WINDOWS_DAEMON_PIPE,
        "--no-overlay",
    ]
    if codex_mcp:
        codex_config += core_agent_config.mcp_server_toml(
            core_mcp.DESKTOP_SERVER,
            cua_driver_path,
            windows_mcp_args,
            env=cua_env,
            # Codex deliberately sanitizes the environment inherited by stdio MCP
            # servers.  Forward the Windows process/profile variables the native
            # bridge needs instead of relying on its launcher's ambient environment.
            env_vars=[
                "APPDATA",
                "COMSPEC",
                "LOCALAPPDATA",
                "PATH",
                "SYSTEMROOT",
                "TEMP",
                "TMP",
                "USERPROFILE",
                "WINDIR",
            ],
            startup_timeout_sec=60,
            tool_timeout_sec=int(mcp_tool_timeout_ms) // 1000,
            required=True,
        )

    codex_config_b64 = base64.b64encode(codex_config.encode("utf-8")).decode("ascii")
    mcp_probe_path = os.path.join(output_dir, "mcp_probe.py")
    agent_exit_path = os.path.join(output_dir, "agent_exit_code.txt")
    shutil.copyfile(
        Path(__file__).resolve().parents[2] / "core" / "mcp_probe.py",
        mcp_probe_path,
    )
    mcp_env_ps1 = "\n".join(
        f"$env:{key} = '{str(value).replace(chr(39), chr(39) * 2)}'"
        for key, value in cua_env.items()
    )
    extra_env_lines = ""

    spec = core_agent_invocation.InvocationSpec(
        agent_cli="codex",
        model=core_agent_config.codex_model_slug(model),
        prompt_file=prompt_path,
        workspace=workspace_dir,
        trajectory_path=trajectory_path,
        stderr_path=stderr_path,
        timeout_sec=timeout,
        request_timeout_ms=int(request_timeout_ms),
        tool_timeout_ms=int(mcp_tool_timeout_ms),
        reasoning_effort=codex_effort,
        executable=shutil.which("codex", path=env.get("PATH")) or "codex",
        capture_tool_use_screenshots=os.environ.get(
            "RB_CAPTURE_TOOL_USE_SCREENSHOTS", "false"
        )
        .strip()
        .lower()
        in ("1", "true", "yes", "on"),
        screenshot_dir=os.path.join(output_dir, "tool_use_screenshots"),
        screenshot_command=(
            VM_PYTHON,
            os.path.join(output_dir, "desktop_capture.py"),
            "{output}",
        ),
    )
    runner_path = os.path.join(output_dir, "agent_invocation.py")
    spec_path = os.path.join(output_dir, "invocation.json")
    shutil.copyfile(Path(core_agent_invocation.__file__), runner_path)
    shutil.copyfile(
        Path(core_agent_invocation.__file__).with_name("tool_use_capture.py"),
        os.path.join(output_dir, "tool_use_capture.py"),
    )
    shutil.copyfile(
        Path(core_agent_invocation.__file__).with_name("desktop_capture.py"),
        os.path.join(output_dir, "desktop_capture.py"),
    )
    core_agent_invocation.write_spec(spec_path, spec)

    wrapper_content = runtime_assets.render_text(
        "windows/runtime/codex_wrapper.ps1",
        {
            "__DEBUG_LOG__": debug_log,
            "__AGENT_EXIT_PATH__": agent_exit_path,
            "__MCP_ENV__": mcp_env_ps1,
            "__EXTRA_ENV__": extra_env_lines,
            "__WORKSPACE_DIR__": workspace_dir,
            "__CODEX_CONFIG_B64__": codex_config_b64,
            "__PROMPT_PATH__": prompt_path,
            "__CUA_PREFLIGHT_MODE__": cua_preflight_mode,
            "__REFERENCE_PID__": int(ref_app_pid),
            "__MCP_PROBE_PATH__": mcp_probe_path,
            "__VM_PYTHON__": VM_PYTHON,
            "__CUA_DRIVER_PATH__": cua_driver_path,
            "__CUA_DAEMON_PIPE__": core_cua_driver.WINDOWS_DAEMON_PIPE,
            "__RUNNER_PATH__": runner_path,
            "__SPEC_PATH__": spec_path,
            "__TRAJECTORY_PATH__": trajectory_path,
            "__STDERR_PATH__": stderr_path,
        },
    )
    wrapper_path = os.path.join(output_dir, "run_codex.ps1")
    Path(wrapper_path).write_text(wrapper_content, encoding="utf-8")
    return wrapper_path


def run(
    task: dict,
    model: str,
    auth_token: str,
    base_url: str,
    timeout: int = 72000,
    dir_suffix: str = "",
    effort: str = "max",
    auto_compact_window: str = "",
    autocompact_pct: str = "",
    cua_coordinate_space: str = "0",
    cua_coordinate_scale: str = "1000",
    mcp_model_payload_filter: str = "0",
    extra_body: str = "",
    agent_cli: str = "claude",
    claude_model: str = "",
    claude_code_max_output_tokens: str = "",
    time_budget_hook: bool = False,
    api_timeout_ms: int = 1800000,
    mcp_tool_timeout_ms: int = 180000,
) -> tuple[str, int]:
    """Run recreation locally on Windows.

    File isolation is always enabled. Network isolation is enabled by default
    and can be disabled explicitly for diagnostics. The agent runs as the
    restricted 'rbagent' user and cannot access the reference binary.

    Args:
        task: Task dict with task_id
        model: Real upstream model name used for stage identity and Codex
        claude_model: Claude Code client alias (possibly context-decorated)
        auth_token: API auth token
        base_url: API base URL
        timeout: Maximum time for the Claude agent (seconds)

    Returns:
        (output_text, exit_code)
    """
    task_id = task["task_id"]
    cua_preflight_mode = (
        os.environ.get("RB_CUA_PREFLIGHT_MODE", "strict").strip().lower()
    )
    if cua_preflight_mode not in {"strict", "warn"}:
        raise ValueError(
            "RB_CUA_PREFLIGHT_MODE must be 'strict' or 'warn', "
            f"got {cua_preflight_mode!r}"
        )
    base = vm_task_dir(task_id)
    reference_dir = os.path.join(base, "reference")
    suffix = f"_{dir_suffix}" if dir_suffix else ""
    rec_dir = os.path.join(base, f"recreation{suffix}")
    output_dir = rec_dir  # stage dir: harness output (prompt, trajectory, sessions)
    os.makedirs(output_dir, exist_ok=True)
    # The agent gets a real, task-name-free cwd.  Restore any partial stage output into it, then
    # snapshot it back only after the agent has stopped; a junction would expose task identity.
    workspace_storage_dir = os.path.join(
        rec_dir, core_recreation_artifact.CANONICAL_INNER
    )

    # --- Check if already done ---
    _done_root = core_recreation_artifact.app_root(rec_dir, "windows")
    build_sh = os.path.join(_done_root, "build.ps1")
    launch_sh = os.path.join(_done_root, "launch.ps1")
    src_dir = os.path.join(_done_root, "src")
    if os.path.exists(build_sh) and os.path.exists(launch_sh):
        src_count = (
            sum(len(files) for _, _, files in os.walk(src_dir))
            if os.path.isdir(src_dir)
            else 0
        )
        if src_count > 0:
            print("  Recreation already done, skipping")
            return "already done", 0

    workspace_dir = _prepare_canonical_workspace(workspace_storage_dir)
    runtime_dir = _prepare_agent_runtime()

    # --- Find build executable ---
    exe_path = _find_exe(reference_dir)
    if not exe_path:
        return "ERROR: no executable found from frozen reference", 1
    print(f"  Reference executable: {exe_path}")

    # --- Copy public frozen fixtures if available ---
    frozen_fixtures = os.path.join(base, "tests", "fixtures")
    ws_fixtures = os.path.join(workspace_dir, "fixtures")
    os.makedirs(ws_fixtures, exist_ok=True)
    if os.path.isdir(frozen_fixtures):
        shutil.copytree(frozen_fixtures, ws_fixtures, dirs_exist_ok=True)
        fixture_count = sum(1 for _ in Path(ws_fixtures).rglob("*") if _.is_file())
        print(f"  Fixtures copied: {fixture_count} files")

    # --- Pre-launch reference app ---
    ref_launch_ps1 = os.path.join(reference_dir, "launch.ps1")
    ref_launch_ps1 = ref_launch_ps1 if os.path.exists(ref_launch_ps1) else None
    _kill_app(exe_path)
    ref_app_pid, _ = _launch_app_shared(exe_path, launch_ps1=ref_launch_ps1, retries=3)
    if ref_app_pid is None:
        return "ERROR: reference app failed to launch", 1

    # Before the preflight, deliberately: the preflight is the thing that fails, and every other
    # screenshot in the pipeline is taken through the MCP bridge it is testing, so a failure left
    # no picture of the reference at all.  One app reported its window as 1x1 and the only way to
    # establish that afterwards was a 17 MB proxy log.
    # Printed rather than recorded in output_lines: that list is not built until much later in
    # this function, and the PNG beside it is the artifact anyway.
    print(
        "  Desktop screenshot: "
        + core_desktop_capture.capture_desktop(
            os.path.join(output_dir, "postlaunch_desktop1.png")
        )
    )

    # The released agent observes through desktop-control MCP.  A fallback to pywinauto would
    # validate a different execution path and turn a broken/missing UIA tree into a scored run.
    cua_path = resolve_cua_driver()
    if not cua_preflight(ref_app_pid, cua_path):
        if os.environ.get("RB_CUA_PREFLIGHT_MODE", "strict").lower() == "warn":
            print(
                "  WARNING: reference app is not observable through desktop-control MCP; "
                "continuing because RB_CUA_PREFLIGHT_MODE=warn"
            )
        else:
            return (
                "ERROR: reference app is not observable through desktop-control MCP",
                1,
            )

    # --- Mode detection ---
    is_codex = agent_cli.startswith("codex")
    claude_client_model = (
        claude_model or core_agent_invocation.DEFAULT_CLAUDE_MODEL_ALIAS
    )
    # Every Codex spelling uses desktop-control MCP. ``codex-mcp`` remains accepted as a
    # backwards-compatible alias; there is no longer a CLI-only observation path.
    codex_mcp = is_codex

    # --- Set API environment ---
    env = os.environ.copy()
    env["NO_PROXY"] = "127.0.0.1,localhost" + (
        f",{env['NO_PROXY']}" if env.get("NO_PROXY") else ""
    )
    env["no_proxy"] = "127.0.0.1,localhost" + (
        f",{env['no_proxy']}" if env.get("no_proxy") else ""
    )
    if not is_codex:
        env["ANTHROPIC_AUTH_TOKEN"] = auth_token
        env["ANTHROPIC_BASE_URL"] = base_url
        env["ANTHROPIC_MODEL"] = claude_client_model
        env["ANTHROPIC_DEFAULT_OPUS_MODEL"] = claude_client_model
        env["ANTHROPIC_DEFAULT_SONNET_MODEL"] = claude_client_model
        env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = claude_client_model
        env["API_FORCE_IDLE_TIMEOUT"] = "0"
        env["API_TIMEOUT_MS"] = str(api_timeout_ms)
        env["CLAUDE_CODE_MAX_RETRIES"] = "0"
        env["MAX_STRUCTURED_OUTPUT_RETRIES"] = "0"
        # Use the shared finite timeout. A tool timeout is recoverable because the agent
        # can report it and retry; an unbounded hang consumes the entire job budget.
        env.update(core_mcp.tool_timeout_env(str(mcp_tool_timeout_ms)))
    else:
        env["OPENAI_API_KEY"] = auth_token

    if claude_code_max_output_tokens:
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = claude_code_max_output_tokens

    if platform.system() == "Windows":
        extra_dirs = [
            r"C:\npm-global",
            r"C:\cargo\bin",
            r"C:\Program Files\Cua\cua-driver\bin",
        ]
        cur_path = env.get("PATH", "").lower()
        for d in extra_dirs:
            if os.path.isdir(d) and d.lower() not in cur_path:
                env["PATH"] = d + os.pathsep + env.get("PATH", "")
                cur_path = env.get("PATH", "").lower()

    # --- Resolve CUA driver before building the Codex MCP configuration. ---
    cua_driver_path = resolve_cua_driver(path=env.get("PATH"))

    # --- Build prompt ---
    prompt = _recreation_prompt.render("windows")
    prompt_path = os.path.join(runtime_dir, "prompt.txt")
    core_agent_invocation.write_prompt(prompt_path, prompt)

    # --- Configure Claude Code settings in workspace ---
    if not is_codex:
        claude_dir = os.path.join(workspace_dir, ".claude")
        os.makedirs(claude_dir, exist_ok=True)
        settings = dict(SETTINGS_JSON)
        # Time-budget forcing-function hooks (Claude only, opt-in).
        if time_budget_hook:
            settings["hooks"] = _write_time_budget_hooks(runtime_dir)
            print(f"  Time-budget hooks enabled (budget={timeout}s)")
        Path(os.path.join(claude_dir, "settings.json")).write_text(
            json.dumps(settings, indent=2), encoding="utf-8"
        )
    # The COORDINATE_*/SESSION_IDLE_TTL/UPDATE_CHECK quartet comes from the ONE shared
    # daemon contract rather than being retyped: windows seeded 2 of the 4, so this
    # driver could self-upgrade mid-benchmark -- the confound the version pin exists to
    # prevent. MCP_MODEL_PAYLOAD_FILTER is genuinely windows-only.
    cua_env = core_mcp.desktop_env(
        normalize=str(cua_coordinate_space) == "1",
        scale=str(cua_coordinate_scale),
        extra={
            "MCP_MODEL_PAYLOAD_FILTER": mcp_model_payload_filter,
            # The Windows bridge must never fall back to an in-process driver in the agent's
            # logon session when the trusted daemon is absent or unhealthy.
            "CUA_DRIVER_RS_MCP_FORCE_PROXY": "1",
        },
    )
    # The agent wrapper is launched with alternate rbagent credentials, so it does not
    # inherit process-scoped variables from the RB_Pipeline scheduled task. Carry the
    # trusted daemon identity into that wrapper explicitly; otherwise strict preflight
    # sees an empty PID even though the daemon is alive in the same interactive session.
    for name in ("RB_CUA_DAEMON_PID", "RB_CUA_DAEMON_SESSION_ID"):
        if value := os.environ.get(name):
            cua_env[name] = value
    windows_mcp_args = [
        "mcp",
        "--socket",
        core_cua_driver.WINDOWS_DAEMON_PIPE,
        "--no-overlay",
    ]
    if not is_codex:
        mcp_config = {
            "mcpServers": {
                "desktop-control": {
                    "command": cua_driver_path,
                    "args": [
                        "mcp",
                        "--socket",
                        core_cua_driver.WINDOWS_DAEMON_PIPE,
                        "--no-overlay",
                        "--claude-code-computer-use-compat",
                    ],
                    "env": cua_env,
                }
            }
        }
    elif codex_mcp:
        mcp_config = {
            "mcpServers": {
                "desktop-control": {
                    "command": cua_driver_path,
                    "args": windows_mcp_args,
                    "env": cua_env,
                }
            }
        }
    else:
        mcp_config = None

    mcp_config_path = os.path.join(runtime_dir, "mcp_config.json")
    if mcp_config:
        Path(mcp_config_path).write_text(
            json.dumps(mcp_config, indent=2), encoding="utf-8"
        )

    # --- Network isolation ---
    network_isolation_enabled = os.environ.get(
        "RB_WINDOWS_ENABLE_NETWORK_ISOLATION", "1"
    ).strip().lower() in ("1", "true", "yes", "on")
    network_state = None
    if network_isolation_enabled:
        print("  Setting up network isolation...")
        try:
            network_state = _setup_network_isolation(base_url, output_dir)
        except BaseException:
            _kill_app(exe_path, pid=ref_app_pid)
            raise
    else:
        print(
            "  WARNING: network isolation disabled "
            "(RB_WINDOWS_ENABLE_NETWORK_ISOLATION is not enabled)"
        )

    # --- File isolation ---
    print("  Setting up file isolation...")
    try:
        isolation_password = _setup_file_isolation(
            base,
            output_dir,
            workspace_dir,
            runtime_dir,
            mcp_config_path,
        )
    except BaseException as setup_error:
        # Network setup used to sit outside the agent's try/finally. A file-ACL failure could
        # therefore strand the VM in block-outbound mode. Close both boundaries here as well.
        if network_state is not None:
            try:
                _restore_network(network_state)
            except RuntimeError as restore_error:
                raise RuntimeError(
                    f"file isolation setup failed ({setup_error}); network rollback also failed "
                    f"({restore_error})"
                ) from setup_error
        _kill_app(exe_path, pid=ref_app_pid)
        raise

    exit_code = 1
    output_lines: list[str] = []
    timed_out = False
    agent_cleanup_error = ""
    network_cleanup_error = ""

    try:

        # --- Run Recreation Agent as rbagent ---
        print(f"  Running Recreation Agent (agent_cli={agent_cli}, timeout={timeout}s)")
        trajectory_path = os.path.join(runtime_dir, "trajectory.jsonl")
        stderr_path = os.path.join(runtime_dir, "stderr.log")
        agent_exit_path = os.path.join(runtime_dir, "agent_exit_code.txt")
        Path(agent_exit_path).unlink(missing_ok=True)
        debug_log = os.path.join(runtime_dir, "agent_debug.log")

        if is_codex:
            wrapper_path = _build_codex_wrapper(
                output_dir=runtime_dir,
                workspace_dir=workspace_dir,
                # The UPSTREAM model, not the Claude Code client alias.  codex has no use for that
                # alias, and passing it meant codex_model_slug() faithfully converted the wrong
                # input: without --claude-model, claude_client_model falls back to a Claude
                # alias that an OpenAI-compatible upstream may reject. Linux selects the
                # Codex slug and Claude alias separately for the same reason.
                model=model,
                effort=effort,
                timeout=timeout,
                codex_base_url=base_url,
                codex_mcp=codex_mcp,
                cua_driver_path=cua_driver_path,
                cua_env=cua_env,
                ref_app_pid=ref_app_pid,
                prompt_path=prompt_path,
                trajectory_path=trajectory_path,
                stderr_path=stderr_path,
                debug_log=debug_log,
                env=env,
                mcp_tool_timeout_ms=mcp_tool_timeout_ms,
                request_timeout_ms=api_timeout_ms,
                cua_preflight_mode=cua_preflight_mode,
            )
        else:
            wrapper_path = _build_claude_wrapper(
                output_dir=runtime_dir,
                workspace_dir=workspace_dir,
                # The Claude Code client alias, decorated with [1m] on the pod when context_1m is
                # on -- NOT the upstream route.  This is the mirror image of the codex branch
                # below, and passing `model` here is what silently capped every windows arm at the
                # CLI's default 200K window.  The alias also has to agree with the four
                # ANTHROPIC_*_MODEL values set above; before this it did not, because the wrapper
                # regenerated its own env from the spec and overwrote them.
                client_model=claude_client_model,
                effort=effort,
                timeout=timeout,
                base_url=base_url,
                mcp_config_path=mcp_config_path,
                auto_compact_window=auto_compact_window,
                autocompact_pct=autocompact_pct,
                claude_code_max_output_tokens=claude_code_max_output_tokens,
                extra_body=extra_body,
                prompt_path=prompt_path,
                trajectory_path=trajectory_path,
                stderr_path=stderr_path,
                debug_log=debug_log,
                env=env,
                cua_driver_path=cua_driver_path,
                cua_env=cua_env,
                ref_app_pid=ref_app_pid,
                cua_preflight_mode=cua_preflight_mode,
                time_budget_hook_sec=(timeout if time_budget_hook else 0),
            )

        for f in (
            wrapper_path,
            prompt_path,
            os.path.join(runtime_dir, "mcp_probe.py"),
            os.path.join(runtime_dir, "agent_invocation.py"),
            os.path.join(runtime_dir, "tool_use_capture.py"),
            os.path.join(runtime_dir, "desktop_capture.py"),
            os.path.join(runtime_dir, "invocation.json"),
        ):
            grant = subprocess.run(
                ["icacls", f, "/grant", "rbagent:(R)", "/Q"],
                capture_output=True,
            )
            if grant.returncode != 0:
                raise RuntimeError(
                    f"could not grant rbagent read access to {f}: rc={grant.returncode}"
                )

        # Set Machine-level env var so rbagent inherits it; clear after launch.
        if is_codex:
            machine_env_name = "OPENAI_API_KEY"
            machine_env_val = auth_token
        else:
            machine_env_name = "ANTHROPIC_AUTH_TOKEN"
            machine_env_val = auth_token
        safe_val = machine_env_val.replace("'", "''")
        _run_ps(
            f"[System.Environment]::SetEnvironmentVariable('{machine_env_name}', '{safe_val}', 'Machine')"
        )
        try:
            launch_ps = (
                f"$pw = ConvertTo-SecureString '{isolation_password}' -AsPlainText -Force;"
                "$cred = New-Object PSCredential('rbagent', $pw);"
                f"$p = Start-Process powershell.exe "
                f"-ArgumentList '-NoProfile -ExecutionPolicy Bypass -File \"{wrapper_path}\"' "
                f"-Credential $cred -LoadUserProfile -PassThru -WindowStyle Hidden "
                f"-WorkingDirectory '{workspace_dir}';"
                "$p.Id"
            )
            r = _run_ps(launch_ps)
            agent_pid = int(r.stdout.strip()) if r.stdout.strip().isdigit() else None
        finally:
            _run_ps(
                f"[System.Environment]::SetEnvironmentVariable('{machine_env_name}', $null, 'Machine')"
            )

        if agent_pid:
            print(f"  Agent running as rbagent (PID={agent_pid})")
            start_time = time.time()
            deadline = start_time + timeout + 120

            def _kill_agent():
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(agent_pid)],
                    capture_output=True,
                )
                time.sleep(5)
                if is_codex:
                    kill_names = "codex*"
                else:
                    kill_names = "claude*"
                subprocess.run(
                    [
                        "powershell",
                        "-Command",
                        f"Get-Process {kill_names} -EA SilentlyContinue | "
                        "Stop-Process -Force -EA SilentlyContinue",
                    ],
                    capture_output=True,
                )

            while True:
                if time.time() > deadline:
                    timed_out = True
                    print(f"  DEADLINE: agent exceeded {timeout}+120s, force exit")
                    _kill_agent()
                    break
                try:
                    check = subprocess.run(
                        ["tasklist", "/FI", f"PID eq {agent_pid}", "/NH"],
                        capture_output=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=10,
                    )
                    if str(agent_pid) not in check.stdout:
                        break
                except Exception:
                    break

                time.sleep(5)
            try:
                exit_code = int(Path(agent_exit_path).read_text().strip())
            except (OSError, ValueError):
                exit_code = core_agent_invocation.INFRA_EXIT_CODE
        else:
            print("  ERROR: failed to launch agent as rbagent")
            print(f"    returncode={r.returncode}")
            print(f"    stdout: {r.stdout[:500] if r.stdout else '(empty)'}")
            print(f"    stderr: {r.stderr[:500] if r.stderr else '(empty)'}")

        if timed_out:
            print(f"  TIMEOUT: shared runner exceeded its {timeout}s budget plus grace")
            output_lines.append(
                f"TIMEOUT: shared runner exceeded its {timeout}s budget plus grace\n"
            )
            exit_code = 124
        verdict = core_agent_invocation.terminal_result(
            trajectory_path, exit_code, timed_out=timed_out
        )
        output_lines.append(f"agent_terminal={verdict.protocol_status}\n")
        output_lines.append(f"agent_verdict={verdict.status}\n")
        exit_code = verdict.exit_code
        print(
            f"  Agent verdict: {verdict.status} "
            f"(protocol={verdict.protocol_status}, native={verdict.native_rc})"
        )

    finally:
        try:
            try:
                _terminate_agent_processes()
            except RuntimeError as exc:
                agent_cleanup_error = str(exc)
                print(f"  ERROR: {agent_cleanup_error}")

            # --- Kill any leftover reference app ---
            _kill_app(exe_path, pid=ref_app_pid)
        finally:
            # Restoring the host policy must not depend on any other cleanup succeeding.
            if network_state is not None:
                try:
                    _restore_network(network_state)
                except RuntimeError as exc:
                    network_cleanup_error = str(exc)
                    print(f"  ERROR: {network_cleanup_error}")

    if agent_cleanup_error:
        # Recorded, and still fatal -- an unclosed agent boundary is a security property, not
        # hygiene, so nothing here may be scored.  But returning at this point also skipped the
        # runtime copy and the snapshot below, which is how one run with
        # `protocol=completed, native=0` reached artifact store holding four files: an agent finished its
        # recreation and a single surviving process erased all of it.  Fall through so the
        # deliverable is preserved for inspection; exit_code is forced to 2 after the snapshot.
        output_lines.append(f"agent_cleanup_error={agent_cleanup_error}\n")
        print(
            "  WARN: snapshotting anyway so the artifact survives; the stage still fails"
        )
    if network_cleanup_error:
        output_lines.append(f"network_cleanup_error={network_cleanup_error}\n")
        print(
            "  WARN: firewall restoration was not attested; preserving artifacts and failing "
            "the stage"
        )

    # Last sweep before anything is copied or deleted, and deliberately after the boundary kill:
    # this catches what OWNER and image-name scoping cannot, and it matters most in exactly the
    # case just above, where the boundary did not close and the copy below now runs anyway.
    swept = _kill_workspace_holders(workspace_dir, runtime_dir)
    if swept:
        output_lines.append(f"workspace_holders_killed={swept}\n")
        print(f"  Swept processes holding the fixed paths: {swept}")

    # --- Save session transcripts to recreation output ---
    # BOTH agent CLIs: Claude Code writes its transcript under .claude\projects, codex writes its
    # "rollout" under CODEX_HOME\sessions. One loop rather than two blocks, so a source cannot be
    # added to one and forgotten in the other. Selection is derived from
    # core.trajectory.EXCLUDE_SUBSTRINGS; robocopy stays the mechanism because it is the only one
    # that handles paths over 260 chars.
    dst = os.path.join(output_dir, "sessions")
    # The wrapper pins CODEX_HOME to the alternate user's profile.  Use the same
    # path here rather than an unrelated value inherited by the trusted worker.
    _codex_home = os.path.join(r"C:\\Users\\rbagent", ".codex")
    for _src in (
        os.path.join(r"C:\\Users\\rbagent", ".claude", "projects"),
        os.path.join(_codex_home, "sessions"),
    ):
        if not os.path.isdir(_src):
            continue
        result = subprocess.run(
            [
                "robocopy",
                _src,
                dst,
                "*.jsonl",
                "/E",
                "/XF",
                # derived, not restated: a substring in core.trajectory.EXCLUDE_SUBSTRINGS
                # becomes the robocopy pattern *substr* so the two cannot drift apart.
                *["*%s*" % x for x in core_trajectory.EXCLUDE_SUBSTRINGS],
                "/R:0",
                "/W:0",
                "/NFL",
                "/NDL",
                "/NJH",
                "/NJS",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode >= 8:
            print(
                f"  WARNING: robocopy sessions failed for {_src} (exit={result.returncode})"
            )
    if os.path.isdir(dst):
        # robocopy did the copying; the NAMING comes from the shared module so windows' output
        # matches the other four platforms instead of keeping raw basenames in robocopy's subtree.
        renamed = core_trajectory.rename_collected(
            dst,
            stage="recreation",
            user="rbagent",
            stream_src=os.path.join(runtime_dir, "trajectory.jsonl"),
        )
        print(f"  sessions/ normalised: {len(renamed)} file(s)")

    # Preserve whether code 86 came from the trusted MCP preflight or from the
    # post-run "no successful tool call" invariant.  The numeric code alone was
    # ambiguous and made the pod retry legitimate model-zero results.
    try:
        debug_text = Path(debug_log).read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        debug_text = ""
    preflight_passed = "MCP reference preflight passed" in debug_text
    output_lines.append(f"mcp_preflight={'pass' if preflight_passed else 'fail'}\n")

    # A compilable guessed app is not a successful recreation when its required observation
    # channel never started. The previous Windows canary returned deployment platform Succeeded and score 0 after
    # every desktop-control call timed out, so make MCP evidence a stage invariant for Codex.
    if is_codex and not codex_desktop_mcp_succeeded(trajectory_path):
        print("  ERROR: Codex completed no successful desktop-control MCP call")
        output_lines.append("desktop_control_mcp=failed\n")
        exit_code = 86
    elif is_codex:
        output_lines.append("desktop_control_mcp=pass\n")

    # Wrappers, logs and authored files use task-free real directories while the agent runs.  Once
    # every process under its token has been terminated, copy them into the task-owned tree, which
    # permission setup already protects from rbagent.  Do not revoke the source ACL first: doing so
    # locked the trusted worker out before shutil could read it.  No junction/symlink is needed.
    try:
        shutil.copytree(runtime_dir, output_dir, dirs_exist_ok=True)
    except OSError as exc:
        output_lines.append(f"runtime_snapshot_error={exc}\n")
        return "".join(output_lines), 2

    # --- Collect results ---
    # Freeze the task-name-free workspace only after the untrusted process has stopped.
    try:
        _snapshot_canonical_workspace(workspace_dir, workspace_storage_dir)
    except OSError as exc:
        output_lines.append(f"workspace_snapshot_error={exc}\n")
        return "".join(output_lines), 2
    # Cleanup happens after the authoritative snapshot has been committed.  A stale
    # handle or ACL must not replace a complete agent result with ``stage_exception``;
    # the next prepare remains strict and will retry before reusing either fixed path.
    _cleanup_snapshotted_tree(workspace_dir, "workspace", output_lines)
    _cleanup_snapshotted_tree(runtime_dir, "runtime", output_lines)
    rec_output = workspace_storage_dir

    # The snapshot is now complete and no untrusted process remains.  Remove any copied agent ACEs
    # from the final task tree while retaining the effective worker, SYSTEM and Administrators.
    try:
        _lock_agent_tree(output_dir)
    except RuntimeError as exc:
        output_lines.append(f"agent_tree_lock_error={exc}\n")
        return "".join(output_lines), 2

    # Self-describing artifact from the shared canonical layout (see
    # core/recreation_artifact.py). The outer stage keys cannot be renamed without
    # orphaning everything already on artifact store, so each artifact carries a pointer instead.
    core_recreation_artifact.write_manifest(rec_output, "windows", ".")
    src_count = 0
    if os.path.isdir(os.path.join(rec_output, "src")):
        src_count = sum(
            len(files) for _, _, files in os.walk(os.path.join(rec_output, "src"))
        )
    bin_count = 0
    if os.path.isdir(os.path.join(rec_output, "bin")):
        bin_count = sum(
            len(files) for _, _, files in os.walk(os.path.join(rec_output, "bin"))
        )
    has_build = os.path.exists(os.path.join(rec_output, "build.ps1"))
    if not has_build and src_count > 0:
        has_build = _autogen_build_ps1(rec_output)
    has_launch = os.path.exists(os.path.join(rec_output, "launch.ps1"))
    has_behavior = os.path.exists(os.path.join(rec_output, "behavior.json"))

    print(
        f"  Results: src={src_count} files, bin={bin_count} files, "
        f"build.ps1={'yes' if has_build else 'no'}, "
        f"launch.ps1={'yes' if has_launch else 'no'}, "
        f"behavior.json={'yes' if has_behavior else 'no'}"
    )

    output_lines.append(f"src_files={src_count}\n")
    output_lines.append(f"bin_files={bin_count}\n")
    output_lines.append(f"has_build_ps1={has_build}\n")
    output_lines.append(f"has_launch_ps1={has_launch}\n")
    output_lines.append(f"has_behavior_json={has_behavior}\n")

    if agent_cleanup_error or network_cleanup_error:
        # Last word, so the codex MCP invariant's 86 above cannot relabel an unclosed boundary as
        # a preflight problem in worker.py's reason_code table.
        exit_code = 2

    output_text = "".join(output_lines)
    return output_text, exit_code
