#!/usr/bin/env python3
"""Cross-platform contract for preparing and attesting an unprivileged agent.

The benchmark deliberately shares one GUI session between the reference app and the
recreation agent.  This module owns the *other* boundary: the agent must run as a
different OS principal, must not be able to read protected material, and must be able
to write its declared workspace/output roots.

``core.permission_probe`` invokes this module with an explicit JSON spec. Permission setup is too
dangerous to infer paths from a working directory or to carry hard-coded task roots.

The implementation is stdlib-only.  POSIX setup/attestation is executable today.
Windows setup uses ``icacls`` and attestation uses ``CreateProcessWithLogonW`` through
PowerShell, because inspecting an ACL is not evidence that the effective user is
actually denied.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path, PureWindowsPath
from typing import Any

# This file is also executed directly by permission_probe.  In that mode Python
# puts scripts/core, rather than scripts, on sys.path; bootstrap the package root
# before importing core so the Windows attestation child works without relying on
# an inherited PYTHONPATH.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

SCHEMA_VERSION = 1
SUPPORTED_PLATFORMS = frozenset({"linux", "windows", "macos", "android", "web"})
DEFAULT_FORBIDDEN_GROUPS = frozenset({"root", "sudo", "wheel", "admin", "docker"})
WINDOWS_PRIVILEGED_GROUP_SIDS = (
    "S-1-5-32-544",  # Administrators
    "S-1-5-32-548",  # Account Operators
    "S-1-5-32-549",  # Server Operators
    "S-1-5-32-550",  # Print Operators
    "S-1-5-32-551",  # Backup Operators
)


class SpecError(ValueError):
    """The requested permission boundary is ambiguous or unsafe to apply."""


def _reject_unknown_keys(raw: dict[str, Any], allowed: set[str], *, where: str) -> None:
    unknown = sorted(set(raw) - allowed)
    if unknown:
        raise SpecError(f"unknown {where} field(s): {', '.join(unknown)}")


def _object(value: Any, *, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SpecError(f"{where} must be an object")
    return value


def _list(value: Any, *, where: str) -> list[Any]:
    if not isinstance(value, list):
        raise SpecError(f"{where} must be an array")
    return value


def _boolean(raw: dict[str, Any], key: str, default: bool, *, where: str) -> bool:
    value = raw.get(key, default)
    if not isinstance(value, bool):
        raise SpecError(f"{where}.{key} must be true or false")
    return value


@dataclass(frozen=True)
class PathRule:
    path: str
    owner: str = ""
    mode: int = 0o700
    recursive: bool = False
    create: bool = False

    @classmethod
    def parse(cls, value: Any, *, default_owner: str, writable: bool) -> PathRule:
        if isinstance(value, str):
            raw: dict[str, Any] = {"path": value}
        elif isinstance(value, dict):
            raw = value
        else:
            raise SpecError(
                f"path rule must be a string or object, got {type(value).__name__}"
            )

        _reject_unknown_keys(
            raw,
            {"path", "owner", "mode", "recursive", "create"},
            where="path rule",
        )
        path_raw = raw.get("path", "")
        if not isinstance(path_raw, str) or not path_raw.strip():
            raise SpecError("path rule has no path")
        path = path_raw.strip()
        mode_raw = raw.get("mode", "0700")
        try:
            if isinstance(mode_raw, bool):
                raise ValueError
            if isinstance(mode_raw, str):
                if not re.fullmatch(r"[0-7]{3,4}", mode_raw):
                    raise ValueError
                mode = int(mode_raw, 8)
            elif isinstance(mode_raw, int):
                mode = mode_raw
            else:
                raise ValueError
        except (TypeError, ValueError) as exc:
            raise SpecError(f"invalid mode for {path!r}: {mode_raw!r}") from exc
        if mode < 0 or mode > 0o777:
            raise SpecError(f"mode for {path!r} is outside 0000..0777")
        owner_raw = raw.get("owner", default_owner)
        if not isinstance(owner_raw, str) or not owner_raw.strip():
            raise SpecError(f"owner for {path!r} must be a non-empty string")
        return cls(
            path=path,
            owner=owner_raw.strip(),
            mode=mode,
            recursive=_boolean(raw, "recursive", False, where=f"path rule {path!r}"),
            create=_boolean(raw, "create", writable, where=f"path rule {path!r}"),
        )


@dataclass(frozen=True)
class PermissionSpec:
    platform: str
    run_id: str
    agent_user: str
    trusted_user: str
    protected: tuple[PathRule, ...]
    writable: tuple[PathRule, ...]
    forbidden_groups: tuple[str, ...] = tuple(sorted(DEFAULT_FORBIDDEN_GROUPS))
    agent_password_env: str = ""

    @classmethod
    def from_dict(
        cls, raw: dict[str, Any], *, expected_platform: str = ""
    ) -> PermissionSpec:
        _reject_unknown_keys(
            raw,
            {"schema_version", "platform", "run_id", "trusted", "agent", "paths"},
            where="top-level",
        )
        if raw.get("schema_version") != SCHEMA_VERSION:
            raise SpecError(
                f"schema_version must be {SCHEMA_VERSION}, got {raw.get('schema_version')!r}"
            )
        platform = str(raw.get("platform", "")).lower()
        if platform not in SUPPORTED_PLATFORMS:
            raise SpecError(f"unsupported platform {platform!r}")
        if expected_platform and platform != expected_platform:
            raise SpecError(
                f"entrypoint is for {expected_platform!r}, spec declares {platform!r}"
            )
        run_id = str(raw.get("run_id", "")).strip()
        if not run_id or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", run_id):
            raise SpecError(
                "run_id must contain only letters, numbers, '.', '_' and '-'"
            )
        agent = _object(raw.get("agent"), where="agent")
        trusted = _object(raw.get("trusted"), where="trusted")
        _reject_unknown_keys(
            agent,
            {"user", "forbidden_groups", "password_env"},
            where="agent",
        )
        _reject_unknown_keys(trusted, {"user"}, where="trusted")
        agent_user = str(agent.get("user", "")).strip()
        trusted_user = str(trusted.get("user", "")).strip()
        if not agent_user or not trusted_user:
            raise SpecError("agent.user and trusted.user are required")
        same_principal = (
            agent_user.casefold() == trusted_user.casefold()
            if platform == "windows"
            else agent_user == trusted_user
        )
        if same_principal:
            raise SpecError("agent.user must differ from trusted.user")

        paths = _object(raw.get("paths"), where="paths")
        _reject_unknown_keys(paths, {"protected", "writable"}, where="paths")
        protected_raw = _list(paths.get("protected"), where="paths.protected")
        writable_raw = _list(paths.get("writable"), where="paths.writable")
        protected = tuple(
            PathRule.parse(v, default_owner=trusted_user, writable=False)
            for v in protected_raw
        )
        writable = tuple(
            PathRule.parse(v, default_owner=agent_user, writable=True)
            for v in writable_raw
        )
        if not protected:
            raise SpecError("at least one protected path is required")
        if not writable:
            raise SpecError("at least one writable path is required")

        _validate_paths(platform, protected, writable)
        groups_raw = _list(
            agent.get("forbidden_groups", sorted(DEFAULT_FORBIDDEN_GROUPS)),
            where="agent.forbidden_groups",
        )
        forbidden_groups = tuple(str(x) for x in groups_raw)
        password_env = str(agent.get("password_env", "")).strip()
        if password_env and not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", password_env):
            raise SpecError("agent.password_env must be an environment-variable name")
        if platform == "windows" and not password_env:
            raise SpecError("windows agent.password_env is required")
        return cls(
            platform=platform,
            run_id=run_id,
            agent_user=agent_user,
            trusted_user=trusted_user,
            protected=protected,
            writable=writable,
            forbidden_groups=forbidden_groups,
            agent_password_env=password_env,
        )


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "required": self.required,
            "detail": self.detail,
        }


@dataclass
class IsolationReport:
    platform: str
    run_id: str
    agent_principal: str
    trusted_principal: str
    phase: str
    checks: list[Check] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return all(c.ok for c in self.checks if c.required)

    def add(self, name: str, ok: bool, detail: str, *, required: bool = True) -> None:
        self.checks.append(Check(name=name, ok=ok, detail=detail, required=required))

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "platform": self.platform,
            "run_id": self.run_id,
            "phase": self.phase,
            "agent_principal": self.agent_principal,
            "trusted_principal": self.trusted_principal,
            "checks": [c.as_dict() for c in self.checks],
            "passed": self.passed,
        }


def load_spec(path: str, *, expected_platform: str = "") -> PermissionSpec:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SpecError(f"cannot read spec {path!r}: {exc}") from exc
    if not isinstance(raw, dict):
        raise SpecError("spec root must be a JSON object")
    return PermissionSpec.from_dict(raw, expected_platform=expected_platform)


def _normalised_path(platform: str, value: str) -> str:
    if platform == "windows":
        p = PureWindowsPath(value)
        if not p.is_absolute() or str(p) in {p.anchor, p.drive + "\\"}:
            raise SpecError(f"refusing broad or relative Windows path {value!r}")
        if ".." in p.parts:
            raise SpecError(f"path traversal is not allowed: {value!r}")
        return str(p).lower()

    p = Path(value)
    if not p.is_absolute():
        raise SpecError(f"path must be absolute: {value!r}")
    # lexical normalisation first: resolve() would follow an attacker-controlled symlink.
    if ".." in p.parts or p == Path("/") or len(p.parts) < 3:
        raise SpecError(f"refusing broad or traversing path {value!r}")
    return os.path.normpath(str(p))


def _is_within(parent: str, child: str, *, windows: bool) -> bool:
    cls = PureWindowsPath if windows else Path
    p, c = cls(parent), cls(child)
    try:
        c.relative_to(p)
        return True
    except ValueError:
        return False


def _validate_paths(
    platform: str, protected: Iterable[PathRule], writable: Iterable[PathRule]
) -> None:
    is_windows = platform == "windows"
    protected_norm = [_normalised_path(platform, r.path) for r in protected]
    writable_norm = [_normalised_path(platform, r.path) for r in writable]
    for p in protected_norm:
        for w in writable_norm:
            if _is_within(p, w, windows=is_windows) or _is_within(
                w, p, windows=is_windows
            ):
                raise SpecError(f"protected and writable roots overlap: {p!r}, {w!r}")


def _walk_no_symlinks(root: Path) -> Iterable[Path]:
    yield root
    if not root.is_dir() or _is_link_or_reparse(root):
        return
    for current, dirs, files in os.walk(root, followlinks=False):
        current_path = Path(current)
        dirs[:] = [d for d in dirs if not _is_link_or_reparse(current_path / d)]
        for name in dirs:
            yield current_path / name
        for name in files:
            p = current_path / name
            if not _is_link_or_reparse(p):
                yield p


def _is_link_or_reparse(path: Path) -> bool:
    """Reject symlinks and Windows junction/reparse points without following them."""

    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode):
        return True
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(attributes & reparse_flag)


def _reject_link_components(path: Path) -> None:
    """Do not let elevated setup escape through a symlinked path component."""

    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if _is_link_or_reparse(current):
            raise SpecError(f"refusing path with symlink/reparse component: {current}")
        if not current.exists():
            # No deeper component can exist before this one is created.
            break


def _posix_identity(user: str) -> tuple[int, int, tuple[str, ...]]:
    import grp
    import pwd

    ent = pwd.getpwnam(user)
    groups = tuple(
        g.gr_name for g in grp.getgrall() if user in g.gr_mem or g.gr_gid == ent.pw_gid
    )
    return ent.pw_uid, ent.pw_gid, groups


def _apply_posix_rule(rule: PathRule) -> None:
    uid, gid, _ = _posix_identity(rule.owner)
    path = Path(rule.path)
    _reject_link_components(path)
    if not path.exists():
        if not rule.create:
            raise SpecError(f"required path does not exist: {rule.path}")
        path.mkdir(parents=True, mode=rule.mode)
    targets = _walk_no_symlinks(path) if rule.recursive else (path,)
    for target in targets:
        if os.chown in os.supports_follow_symlinks:
            os.chown(target, uid, gid, follow_symlinks=False)
        else:
            os.chown(target, uid, gid)
        # For recursive rules, preserve executable bits on files instead of making every file 0700.
        mode = rule.mode
        if rule.recursive and target.is_file() and not (target.stat().st_mode & 0o111):
            mode &= ~0o111
        if os.chmod in os.supports_follow_symlinks:
            os.chmod(target, mode, follow_symlinks=False)
        else:
            # All link/reparse roots and descendants were rejected or skipped above.
            os.chmod(target, mode)


def _run_as_posix(user: str, argv: list[str]) -> subprocess.CompletedProcess[str]:
    uid, gid, _ = _posix_identity(user)
    if os.geteuid() not in (0, uid):
        raise PermissionError(
            f"must run as root or {user!r} to attest effective access"
        )

    def demote() -> None:
        if os.geteuid() == 0:
            os.setgroups([])
            os.setgid(gid)
            os.setuid(uid)

    return subprocess.run(argv, capture_output=True, text=True, preexec_fn=demote)


def _probe_posix_protected_access(user: str, path: str) -> bool:
    """Return true if the agent can read, modify, or execute/traverse a protected root."""

    for permission in ("-r", "-w", "-x"):
        # ``test`` is /bin/test on the stock macOS image.  Keeping an absolute path
        # avoids running anything from the demoted user's PATH, but /usr/bin/test made
        # every protected path fail with ENOENT before its permissions were examined.
        if _run_as_posix(user, ["/bin/test", permission, path]).returncode == 0:
            return True
    return False


def _probe_posix_write(user: str, path: str, run_id: str) -> bool:
    marker = str(Path(path) / f".rb-permission-probe-{run_id}-{os.getpid()}")
    result = _run_as_posix(user, ["/usr/bin/touch", marker])
    try:
        Path(marker).unlink(missing_ok=True)
    except OSError:
        pass
    return result.returncode == 0


def _windows_quote(value: str) -> str:
    return value.replace("'", "''")


def _windows_impersonate_prelude() -> str:
    """Load the Windows-only helper without imposing it on shipped POSIX probes."""
    from core import runtime_assets

    return runtime_assets.load_text("core/assets/windows_impersonation.ps1")


def _run_windows_as_impersonated(
    spec: PermissionSpec, body: str
) -> subprocess.CompletedProcess[str] | None:
    """Run ``body`` under the agent's token, in-process. Returns None if the logon cannot be made.

    This is the route that works, and the reason the other two do not is worth stating. Every probe
    here is a LOCAL FILE ACCESS CHECK, but both earlier mechanisms tried to *launch a process*
    as the
    agent, and each is blocked for a different reason on an SSH-driven windows VM:

    * ``Start-Process -Credential`` needs a window station and desktop the new user can attach to.
      There is none, so it dies with 0xC0000142 before the body runs (canary winsetup9).
    * a scheduled task needs the Task Scheduler to actually start it. With SeBatchLogonRight granted
      and both /create and /run reporting success, it still never executed -- the poll gave up on
      ``status='ready' result=267011``, i.e. SCHED_S_TASK_HAS_NOT_RUN (winsetup16-17).

    LogonUser + ImpersonateLoggedOnUser needs neither: no desktop, no scheduler, no user
    profile. The
    token is obtained with LOGON32_LOGON_BATCH, and file access under impersonation is exactly what
    the boundary is about.
    """
    if not spec.agent_password_env:
        raise SpecError("windows agent.password_env is required")
    if not os.environ.get(spec.agent_password_env, ""):
        raise SpecError(f"{spec.agent_password_env} is empty")
    script = (
        _windows_impersonate_prelude()
        % {"pwenv": spec.agent_password_env, "user": spec.agent_user}
        + body
    )
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-EncodedCommand", encoded],
        capture_output=True,
        text=True,
    )
    if proc.returncode in (240, 241):
        # A logon that cannot be made is NOT a denial. Returning it as one would make every negative
        # probe pass for the wrong reason -- the false-pass that made winsetup6-8 look clean.
        print(
            f"[permission] impersonation unavailable (rc={proc.returncode}): "
            f"{(proc.stdout + proc.stderr).strip()[:300]}",
            file=sys.stderr,
        )
        return None
    return proc


def _run_windows_as_batch(
    spec: PermissionSpec, body: str
) -> subprocess.CompletedProcess[str] | None:
    """Run ``body`` as the agent through a scheduled task, i.e. a BATCH logon.

    Returns None when the task could not be created or run at all, so the caller can fall back;
    returns the task's own exit code otherwise. A batch logon needs no window station, which is the
    whole point -- see the comment in _run_windows_as.

    Created with /np, so NO password touches a command line: the scheduler issues an S4U token,
    a batch logon obtained without a secret.
    """
    task = f"rb_perm_{os.getpid()}"
    encoded = base64.b64encode(body.encode("utf-16le")).decode("ascii")
    action = f"powershell.exe -NoProfile -EncodedCommand {encoded}"
    create = subprocess.run(
        [
            "schtasks",
            "/create",
            "/tn",
            task,
            "/tr",
            action,
            "/sc",
            "once",
            "/st",
            "00:00",
            "/ru",
            spec.agent_user,
            "/f",
        ],
        capture_output=True,
        text=True,
        # The password goes on STDIN, never in argv. schtasks prompts "Please enter the run as
        # password" (winsetup12 proved /np is not honoured on this build and the prompt is what
        # refused it), and answering the prompt keeps the secret off every command line -- which is
        # the invariant test_windows_probe_does_not_put_password_on_command_line exists to protect,
        # and `/rp <password>` would break.
        input=os.environ.get(spec.agent_password_env, "") + "\n",
    )
    if create.returncode != 0:
        # A refusal here is usually the "Log on as a batch job" right or an /np-incompatible policy.
        # Printing it is what makes the difference between a diagnosis and another guess.
        print(
            f"[permission] schtasks /create refused (rc={create.returncode}): "
            f"{(create.stdout + create.stderr).strip()[:300]}",
            file=sys.stderr,
        )
        return None
    try:
        run = subprocess.run(
            ["schtasks", "/run", "/tn", task], capture_output=True, text=True
        )
        if run.returncode != 0:
            # The last path in this function without a diagnostic, and therefore the one winsetup14
            # landed on: exit=1 with no [permission] line at all, which was the only way left
            # to tell
            # that /run had refused and the interactive fallback had answered.
            print(
                f"[permission] schtasks /run refused (rc={run.returncode}): "
                f"{(run.stdout + run.stderr).strip()[:300]}",
                file=sys.stderr,
            )
            return None
        # /query is the only way to read the result; the task is brief but not instantaneous.
        for _ in range(60):
            q = subprocess.run(
                ["schtasks", "/query", "/tn", task, "/fo", "list", "/v"],
                capture_output=True,
                text=True,
            )
            text = q.stdout or ""
            status = ""
            result = None
            for line in text.splitlines():
                low = line.casefold()
                if low.startswith("status:"):
                    status = line.split(":", 1)[1].strip().casefold()
                elif "last result" in low:
                    tail = line.split(":", 1)[1].strip()
                    try:
                        result = int(tail, 0)
                    except ValueError:
                        result = None
            # 267011 = 0x41303 SCHED_S_TASK_HAS_NOT_RUN, 267009 = 0x41301 SCHED_S_TASK_RUNNING.
            # /run returns immediately and the task reports status='ready' with the HAS_NOT_RUN
            # placeholder until it actually executes, so a naive "not running and has a result" test
            # reads the placeholder as the answer -- winsetup13 returned exit=267011 that way.
            if result in (267011, 267009):
                time.sleep(1)
                continue
            if status and status != "running" and result is not None:
                print(
                    f"[permission] schtasks task finished status={status!r} last_result={result}",
                    file=sys.stderr,
                )
                return subprocess.CompletedProcess([], result, "", "")
            time.sleep(1)
        # The only branch left that said nothing, and so the one winsetup16 landed on: the right
        # was granted and both /create and /run succeeded, yet the task never produced a result.
        # Report the LAST thing observed rather than a bare None.
        print(
            f"[permission] schtasks poll gave up: last status={status!r} result={result}",
            file=sys.stderr,
        )
        print(
            f"[permission] last /query output: {(q.stdout or '')[:600]}",
            file=sys.stderr,
        )
        return None
    finally:
        subprocess.run(
            ["schtasks", "/delete", "/tn", task, "/f"],
            capture_output=True,
            text=True,
        )


def _run_windows_as(
    spec: PermissionSpec, body: str
) -> subprocess.CompletedProcess[str]:
    if not spec.agent_password_env:
        raise SpecError(
            "windows agent.password_env is required for effective-user attestation"
        )
    if not os.environ.get(spec.agent_password_env, ""):
        raise SpecError(f"{spec.agent_password_env} is empty")
    # Start-Process -Credential launches an INTERACTIVE process, which needs a window station and
    # desktop the new user can attach to. Over SSH there is none, so it dies with 0xC0000142
    # (STATUS_DLL_INIT_FAILED) before the body runs -- and because a probe that cannot start also
    # cannot read a protected path, that failure masqueraded as a clean boundary: canaries
    # winsetup6-8 reported both protected roots "denied" while the write probe failed. A scheduled
    # task runs under a BATCH logon, which needs no desktop, so that is tried first and the
    # interactive route stays as the fallback for a session that does have one.
    # In-process impersonation first: it needs no desktop, no scheduler and no profile, which is
    # what
    # defeated both process-launching routes below. See _run_windows_as_impersonated.
    impersonated = _run_windows_as_impersonated(spec, body)
    if impersonated is not None:
        return subprocess.CompletedProcess(
            [],
            impersonated.returncode,
            impersonated.stdout,
            f"[route=impersonate] {impersonated.stderr}".strip(),
        )
    batch = _run_windows_as_batch(spec, body)
    if batch is not None:
        # Which ROUTE ran, and what the task actually reported. winsetup11 returned exit=1 with
        # no way
        # to tell a task that ran and failed from a fallback to the interactive route -- the same
        # missing-diagnostic shape that made 0xC0000142 and icacls 1332 unknowable until they were
        # printed. stderr is the channel because the caller only reads returncode.
        return subprocess.CompletedProcess(
            [], batch.returncode, batch.stdout, f"[route=batch] {batch.stderr}".strip()
        )
    print(
        "[permission] batch route unavailable; falling back to an interactive logon",
        file=sys.stderr,
    )
    # The child writes only a one-line result to a trusted temporary path. Start-Process returns
    # before stdout can be captured, so -Wait plus this file is the most portable built-in route.
    fd, result_path = tempfile.mkstemp(prefix="rb-permission-win-", suffix=".txt")
    os.close(fd)
    encoded = base64.b64encode(body.encode("utf-16le")).decode("ascii")
    ps = (
        f"$plain=[Environment]::GetEnvironmentVariable("
        f"'{_windows_quote(spec.agent_password_env)}','Process');"
        "$pw=ConvertTo-SecureString $plain -AsPlainText -Force;"
        f"$cred=New-Object PSCredential('{_windows_quote(spec.agent_user)}',$pw);"
        f"$p=Start-Process powershell.exe -Credential $cred -LoadUserProfile -Wait -PassThru "
        f"-UseNewEnvironment -ArgumentList '-NoProfile','-EncodedCommand','{encoded}';"
        f"Set-Content -LiteralPath '{_windows_quote(result_path)}' -Value $p.ExitCode"
    )
    try:
        outer = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps], capture_output=True, text=True
        )
        if outer.returncode != 0:
            return outer
        rc_text = Path(result_path).read_text(errors="replace").strip()
        rc = int(rc_text) if rc_text.isdigit() else 1
        return subprocess.CompletedProcess([], rc, "", "")
    finally:
        Path(result_path).unlink(missing_ok=True)


def _apply_windows_rule(
    spec: PermissionSpec, rule: PathRule, *, writable: bool
) -> None:
    path = Path(rule.path)
    _reject_link_components(path)
    if not path.exists():
        if not rule.create:
            raise SpecError(f"required path does not exist: {rule.path}")
        path.mkdir(parents=True)
    targets = _walk_no_symlinks(path) if rule.recursive else (path,)
    for target in targets:
        inheritance = "(OI)(CI)" if target.is_dir() else ""
        operations = [["/setowner", rule.owner]]
        if writable:
            operations.append(["/grant:r", f"{rule.owner}:{inheritance}F"])
        else:
            operations.extend(
                [
                    ["/grant:r", f"{rule.owner}:{inheritance}F"],
                    ["/deny", f"{spec.agent_user}:{inheritance}F"],
                ]
            )
        for operation in operations:
            result = subprocess.run(
                ["icacls", str(target), *operation, "/Q"],
                capture_output=True,
                text=True,
            )
            if result.returncode != 0:
                raise RuntimeError(
                    f"icacls {' '.join(operation)} failed for {target}: "
                    f"{result.stderr.strip()}"
                )


def setup(spec: PermissionSpec) -> IsolationReport:
    report = IsolationReport(
        platform=spec.platform,
        run_id=spec.run_id,
        agent_principal=spec.agent_user,
        trusted_principal=spec.trusted_user,
        phase="setup",
    )
    try:
        if spec.platform == "windows":
            for rule in spec.protected:
                _apply_windows_rule(spec, rule, writable=False)
            for rule in spec.writable:
                _apply_windows_rule(spec, rule, writable=True)
        else:
            if os.geteuid() != 0:
                raise PermissionError("permission setup must run as root")
            for rule in (*spec.protected, *spec.writable):
                _apply_posix_rule(rule)
        report.add("permissions_applied", True, "all declared path rules applied")
    except Exception as exc:
        report.add("permissions_applied", False, str(exc))
        return report

    verified = attest(spec)
    report.checks.extend(verified.checks)
    return report


def attest(spec: PermissionSpec) -> IsolationReport:
    report = IsolationReport(
        platform=spec.platform,
        run_id=spec.run_id,
        agent_principal=spec.agent_user,
        trusted_principal=spec.trusted_user,
        phase="attest",
    )
    if spec.platform == "windows":
        agent_exists = (
            subprocess.run(
                ["net", "user", spec.agent_user], capture_output=True, text=True
            ).returncode
            == 0
        )
        trusted_exists = (
            subprocess.run(
                ["net", "user", spec.trusted_user], capture_output=True, text=True
            ).returncode
            == 0
        )
        report.add("agent_principal_exists", agent_exists, spec.agent_user)
        report.add("trusted_principal_exists", trusted_exists, spec.trusted_user)
        if not agent_exists or not trusted_exists:
            return report
        try:
            forbidden_sids = ",".join(
                f"'{sid}'" for sid in WINDOWS_PRIVILEGED_GROUP_SIDS
            )
            forbidden_names = ",".join(
                f"'{_windows_quote(name.casefold())}'" for name in spec.forbidden_groups
            )
            unprivileged_body = (
                "$groups=@([Security.Principal.WindowsIdentity]::GetCurrent().Groups);"
                "$ids=@($groups | ForEach-Object {$_.Value});"
                f"$forbidden=@({forbidden_sids});"
                "$names=@($groups | ForEach-Object {try {"
                "$_.Translate([Security.Principal.NTAccount]).Value.ToLowerInvariant()"
                "} catch {''}});"
                f"$forbiddenNames=@({forbidden_names});"
                "$badNames=@($forbiddenNames | Where-Object {"
                "$wanted=$_; @($names | Where-Object {$_ -eq $wanted -or "
                "$_.EndsWith('\\'+$wanted)}).Count -gt 0});"
                "if (@($forbidden | Where-Object {$ids -contains $_}).Count -gt 0 -or "
                "$badNames.Count -gt 0) "
                "{ exit 1 } else { exit 0 }"
            )
            unprivileged = _run_windows_as(spec, unprivileged_body).returncode == 0
            report.add(
                "agent_is_unprivileged",
                unprivileged,
                (
                    "token has no privileged local-group SID"
                    if unprivileged
                    else "token belongs to a privileged local group"
                ),
            )
        except Exception as exc:
            report.add("agent_is_unprivileged", False, str(exc))
        for rule in spec.protected:
            path = Path(rule.path)
            if not path.exists():
                report.add(
                    f"protected:{rule.path}", False, "protected path does not exist"
                )
                continue
            quoted = _windows_quote(rule.path)
            if path.is_dir():
                marker = str(
                    PureWindowsPath(rule.path)
                    / f".rb-protected-probe-{spec.run_id}-{os.getpid()}"
                )
                body = (
                    "$exposed=$false;"
                    f"try {{ Get-ChildItem -LiteralPath '{quoted}' -Force -ErrorAction Stop "
                    "| Select-Object -First 1 | Out-Null; $exposed=$true } catch {};"
                    f"try {{ Set-Content -LiteralPath '{_windows_quote(marker)}' -Value x "
                    "-ErrorAction Stop; $exposed=$true } catch {};"
                    f"Remove-Item -LiteralPath '{_windows_quote(marker)}' -Force "
                    "-ErrorAction SilentlyContinue;"
                    "if ($exposed) { exit 0 } else { exit 1 }"
                )
            else:
                body = (
                    "$exposed=$false;"
                    f"try {{ $s=[IO.File]::OpenRead('{quoted}'); $s.Dispose(); "
                    "$exposed=$true } catch {};"
                    f"try {{ $s=[IO.File]::Open('{quoted}',[IO.FileMode]::Open,"
                    "[IO.FileAccess]::Write,[IO.FileShare]::None); $s.Dispose(); "
                    "$exposed=$true } catch {};"
                    "if ($exposed) { exit 0 } else { exit 1 }"
                )
            try:
                exposed = _run_windows_as(spec, body).returncode == 0
                report.add(
                    f"protected:{rule.path}",
                    not exposed,
                    (
                        "effective-user access denied"
                        if not exposed
                        else "effective-user access succeeded"
                    ),
                )
            except Exception as exc:
                report.add(f"protected:{rule.path}", False, str(exc))
        for rule in spec.writable:
            marker = str(
                PureWindowsPath(rule.path) / f".rb-permission-probe-{spec.run_id}"
            )
            body = (
                f"try {{ Set-Content -LiteralPath '{_windows_quote(marker)}' -Value x "
                "-ErrorAction Stop; "
                f"Remove-Item -LiteralPath '{_windows_quote(marker)}' -Force; "
                "exit 0 } catch { exit 1 }"
            )
            try:
                writable = _run_windows_as(spec, body).returncode == 0
                report.add(f"writable:{rule.path}", writable, "effective write probe")
            except Exception as exc:
                report.add(f"writable:{rule.path}", False, str(exc))
        return report

    try:
        trusted_uid, _trusted_gid, _trusted_groups = _posix_identity(spec.trusted_user)
        report.add("trusted_principal_exists", True, f"uid={trusted_uid}")
    except Exception as exc:
        report.add("trusted_principal_exists", False, str(exc))
        return report

    try:
        uid, _gid, groups = _posix_identity(spec.agent_user)
        report.add(
            "agent_principal_exists", True, f"uid={uid} groups={','.join(groups)}"
        )
        report.add("agent_is_unprivileged", uid != 0, f"uid={uid}")
        forbidden = {group.casefold() for group in spec.forbidden_groups}
        bad_groups = sorted(group for group in groups if group.casefold() in forbidden)
        report.add(
            "agent_has_no_privileged_groups",
            not bad_groups,
            "none" if not bad_groups else ",".join(bad_groups),
        )
    except Exception as exc:
        report.add("agent_principal_exists", False, str(exc))
        return report

    for rule in spec.protected:
        if not Path(rule.path).exists():
            report.add(f"protected:{rule.path}", False, "protected path does not exist")
            continue
        try:
            exposed = _probe_posix_protected_access(spec.agent_user, rule.path)
            report.add(
                f"protected:{rule.path}",
                not exposed,
                (
                    "effective-user access denied"
                    if not exposed
                    else "effective-user access succeeded"
                ),
            )
        except Exception as exc:
            report.add(f"protected:{rule.path}", False, str(exc))
    for rule in spec.writable:
        try:
            writable = _probe_posix_write(spec.agent_user, rule.path, spec.run_id)
            report.add(f"writable:{rule.path}", writable, "effective-user write probe")
        except Exception as exc:
            report.add(f"writable:{rule.path}", False, str(exc))
    return report


def _write_report(report: IsolationReport, path: str) -> None:
    payload = json.dumps(report.as_dict(), indent=2, ensure_ascii=False) + "\n"
    if not path or path == "-":
        sys.stdout.write(payload)
        return
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=target.name + ".", dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp, target)
    finally:
        Path(tmp).unlink(missing_ok=True)


def main(argv: list[str] | None = None, *, expected_platform: str = "") -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("setup", "attest"))
    parser.add_argument("--spec", required=True)
    parser.add_argument("--report", default="-")
    parser.add_argument("--platform", default=expected_platform)
    args = parser.parse_args(argv)
    expected = expected_platform or args.platform
    try:
        spec = load_spec(args.spec, expected_platform=expected)
        if args.action == "setup":
            report = setup(spec)
            _write_report(report, args.report)
            return 0 if report.passed else 2
        if args.action == "attest":
            report = attest(spec)
            _write_report(report, args.report)
            return 0 if report.passed else 2
    except SpecError as exc:
        print(f"permission setup: {exc}", file=sys.stderr)
        return 64


if __name__ == "__main__":
    raise SystemExit(main())
