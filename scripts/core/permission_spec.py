"""Build a permission spec for any platform, and create the principal it names.

``core/permission_setup.py`` deliberately does not create principals — it applies and ATTESTS rules
against an identity that already exists. Every platform still has to produce that identity and say
which paths matter, and those two jobs are identical everywhere except for the path list. Keeping
them here means a new platform writes a path list, not a fourth copy of useradd handling.

Platform-specific values are selected by ``core.permission_probe``. Account creation and cache
hand-over stay here so every setup lane uses one implementation.
"""

from __future__ import annotations

import os
import pathlib
import secrets
import shutil
import subprocess

# Groups that would make the file boundary decorative. `root` is the one that matters; the rest
# carried so a base image that quietly adds sudo/docker membership fails attestation instead of
# regaining authority. `admin` is not hypothetical: macOS creates its agent with `-admin` today.
FORBIDDEN_GROUPS = ("root", "sudo", "wheel", "admin", "docker", "adm")

DEFAULT_AGENT_USER = "rbagent"
WINDOWS_PASSWORD_ENV = "RB_AGENT_PASSWORD"
WINDOWS_PRIVILEGED_GROUPS = ("Administrators", "Power Users", "Backup Operators")
_WINDOWS_MAX_PASSWORD = 14


def _run(argv: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(argv, capture_output=True, text=True)
    except (FileNotFoundError, OSError) as exc:
        return 127, f"{argv[0]}: {exc}"
    return p.returncode, (p.stdout + p.stderr).strip()


def ensure_agent_user(user: str = DEFAULT_AGENT_USER) -> dict:
    """Create ``user`` if absent. Idempotent; reports what it did.

    POSIX only, hence the local import: ``pwd`` does not exist on windows, and importing it at
    module level made this whole file unimportable there. Windows reaches it through
    scripts/windows/permission_spec.py (which needs `assemble` and the group list) and died with
    ModuleNotFoundError before running anything. Windows creates its own
    principal with `net user`, so it never calls this.
    """
    import pwd

    try:
        pwd.getpwnam(user)
        return {"created": False, "user": user, "detail": "already exists"}
    except KeyError:
        pass
    home = f"/home/{user}"
    last = "no useradd/adduser available"
    for argv in (
        ["useradd", "--create-home", "--home-dir", home, "--shell", "/bin/bash", user],
        ["adduser", "--disabled-password", "--gecos", "", "--home", home, user],
    ):
        if shutil.which(argv[0]) is None:
            continue
        rc, out = _run(argv)
        if rc == 0:
            return {"created": True, "user": user, "home": home, "via": argv[0]}
        last = out
    raise SystemExit(f"could not create agent principal {user!r}: {last}")


def generate_windows_password() -> str:
    """Return a non-interactive, complexity-safe Windows local-account password."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
    body = "".join(secrets.choice(alphabet) for _ in range(_WINDOWS_MAX_PASSWORD - 4))
    return (
        secrets.choice("ABCDEFGHJKLMNPQRSTUVWXYZ")
        + secrets.choice("abcdefghijkmnopqrstuvwxyz")
        + secrets.choice("23456789")
        + body
        + "-"
    )


def _windows_user_sid(user: str) -> str:
    escaped_user = user.replace("'", "''")
    rc, out = _run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"(Get-LocalUser -Name '{escaped_user}').SID.Value",
        ]
    )
    if rc != 0:
        return ""
    return next((line.strip() for line in out.splitlines() if line.strip()), "")


def _has_windows_user_right(text: str, key: str, user: str, sid: str = "") -> bool:
    expected = {user.casefold()}
    if sid:
        expected.add(sid.casefold())
    for line in text.splitlines():
        name, separator, value = line.partition("=")
        if not separator or name.strip().casefold() != key.casefold():
            continue
        for entry in value.split(","):
            principal = entry.strip().lstrip("*").casefold()
            if principal in expected or principal.endswith("\\" + user.casefold()):
                return True
    return False


def grant_windows_batch_logon(user: str) -> dict:
    """Grant the local account the batch-logon right used by permission attestation."""
    tmp = pathlib.Path(os.environ.get("TEMP", "."))
    token = f"{os.getpid()}_{secrets.token_hex(4)}"
    export = tmp / f"rb_rights_{token}.inf"
    apply_inf = tmp / f"rb_rights_{token}_new.inf"
    verify_inf = tmp / f"rb_rights_{token}_verify.inf"
    db = tmp / f"rb_rights_{token}.sdb"
    log = tmp / f"rb_rights_{token}.log"
    rc, out = _run(
        ["secedit", "/export", "/cfg", str(export), "/areas", "USER_RIGHTS", "/quiet"]
    )
    if rc != 0 or not export.exists():
        return {"granted": False, "detail": f"secedit /export rc={rc}: {out[:200]}"}
    text = export.read_text(encoding="utf-16", errors="replace")
    key = "SeBatchLogonRight"
    sid = _windows_user_sid(user)
    if _has_windows_user_right(text, key, user, sid):
        return {"granted": True, "detail": "already granted"}

    lines, found = [], False
    for line in text.splitlines():
        if line.strip().lower().startswith(key.lower()):
            found = True
            line = line.rstrip() + f",*{sid}" if sid else line.rstrip() + f",{user}"
        lines.append(line)
    if not found:
        for index, line in enumerate(lines):
            if line.strip().lower() == "[privilege rights]":
                principal = f"*{sid}" if sid else user
                lines.insert(index + 1, f"{key} = {principal}")
                found = True
                break
    if not found:
        return {"granted": False, "detail": "no [Privilege Rights] section in export"}
    apply_inf.write_text("\r\n".join(lines) + "\r\n", encoding="utf-16")
    configure_rc, configure_out = _run(
        [
            "secedit",
            "/configure",
            "/db",
            str(db),
            "/cfg",
            str(apply_inf),
            "/areas",
            "USER_RIGHTS",
            "/log",
            str(log),
            "/quiet",
        ]
    )
    verify_rc, verify_out = _run(
        [
            "secedit",
            "/export",
            "/cfg",
            str(verify_inf),
            "/areas",
            "USER_RIGHTS",
            "/quiet",
        ]
    )
    granted = False
    if verify_rc == 0 and verify_inf.exists():
        verified = verify_inf.read_text(encoding="utf-16", errors="replace")
        granted = _has_windows_user_right(verified, key, user, sid)
    return {
        "granted": granted,
        "detail": (
            f"configured and verified (secedit rc={configure_rc})"
            if granted
            else f"secedit /configure rc={configure_rc}: "
            f"{configure_out[:120]}; verify rc={verify_rc}: {verify_out[:120]}"
        ),
    }


def ensure_windows_agent_user(user: str = DEFAULT_AGENT_USER) -> dict:
    """Create/reset one unprivileged Windows account without exposing its secret."""
    password = generate_windows_password()
    rc, out = _run(["net", "user", user])
    if rc != 0:
        rc, out = _run(["net", "user", user, password, "/add"])
        created = True
    else:
        rc, out = _run(["net", "user", user, password])
        created = False
    if rc != 0:
        raise SystemExit(
            f"could not create/reset agent principal {user!r}: {out[:300]}"
        )

    _run(["net", "user", user, "/logonpasswordchg:no"])
    _run(
        [
            "wmic",
            "useraccount",
            "where",
            f"name='{user}'",
            "set",
            "PasswordExpires=FALSE",
        ]
    )
    removed, delete_errors = [], {}
    for group in WINDOWS_PRIVILEGED_GROUPS:
        rc, detail = _run(["net", "localgroup", group, user, "/delete"])
        if rc == 0:
            removed.append(group)
        else:
            delete_errors[group] = detail[:120]
    batch = grant_windows_batch_logon(user)
    _, detail = _run(["net", "user", user])
    memberships = [
        line.strip()
        for line in detail.splitlines()
        if "group membership" in line.casefold()
    ]
    # The child attestation reads the secret from its environment. It never enters the spec,
    # transcript, result JSON, or process argv.
    os.environ[WINDOWS_PASSWORD_ENV] = password
    return {
        "created": created,
        "user": user,
        "removed_from": removed,
        "delete_errors": delete_errors,
        "memberships": memberships,
        "batch_logon": batch,
        "password_env": WINDOWS_PASSWORD_ENV,
    }


def hand_over_cache(user: str, candidates: list[str], label: str) -> dict:
    """Give the agent a build cache it would otherwise rebuild inside a wall-clocked run.

    Discovered rather than assumed: these locations belong to the image, not to this repo.

    Chowning in place is NOT enough, and this is the trap worth remembering. Caches live under
    ``/root``, mode 0700 -- so after a perfect ``chown -R`` the agent still cannot reach the
    directory, because open() needs +x on every path component. The web setup probe reported
    exactly that: "NPM CACHE NOT READABLE BY THE AGENT: /root/.npm". So the cache is RELOCATED
    under the agent's own home, which needs no widening of root's.

    A rename is free on one filesystem and the fallback is a copy; if both fail the agent gets a
    cold cache, reported rather than hidden, because a cold cache presents as a slow model.
    """
    import shutil

    for cache in candidates:
        if not cache or not os.path.isdir(cache):
            continue
        dest = f"/home/{user}/{os.path.basename(cache)}"
        moved, detail = False, ""
        if os.path.abspath(cache) == os.path.abspath(dest):
            moved = True
            detail = "already in the agent's home"
        else:
            try:
                if os.path.exists(dest):
                    shutil.rmtree(dest, ignore_errors=True)
                os.rename(cache, dest)
                moved, detail = True, "renamed"
            except OSError:
                try:
                    shutil.copytree(cache, dest, dirs_exist_ok=True)
                    moved, detail = True, "copied (different filesystem)"
                except OSError as exc:
                    detail = f"could not relocate: {exc}"
        target = dest if moved else cache
        rc, out = _run(["chown", "-R", f"{user}:{user}", target])
        return {
            "label": label,
            "cache": target,
            "relocated": moved,
            "chowned": rc == 0,
            "detail": detail or (out[:200] if rc else "ok"),
            # The agent must be TOLD, or it silently starts a cold cache in its new HOME.
            "env_value": target if moved and rc == 0 else "",
        }
    return {
        "label": label,
        "cache": None,
        "relocated": False,
        "chowned": False,
        "detail": f"no {label} cache found to hand over",
        "env_value": "",
    }


def assemble(
    platform: str,
    run_id: str,
    *,
    protected: list[str],
    writable: list[str],
    writable_mode: str = "0700",
    user: str = DEFAULT_AGENT_USER,
    trusted: str = "root",
) -> dict:
    """Turn two path lists into the spec ``core/permission_setup.py`` consumes.

    ``protected`` becomes root-owned 0700 recursive; ``writable`` becomes agent-owned, recursive,
    uses ``writable_mode``, and is created if missing. Overlap is rejected by the setup module at
    exit 64, so a caller that nests one inside the other fails every run rather than being silently
    un-isolated — check the lists rather than relying on that.
    """
    agent = {"user": user, "forbidden_groups": list(FORBIDDEN_GROUPS)}
    if platform == "windows":
        agent["forbidden_groups"] = list(WINDOWS_PRIVILEGED_GROUPS)
        agent["password_env"] = WINDOWS_PASSWORD_ENV
    return {
        "schema_version": 1,
        "platform": platform,
        "run_id": run_id,
        "trusted": {"user": trusted},
        "agent": agent,
        "paths": {
            "protected": [
                {"path": p, "owner": trusted, "mode": "0700", "recursive": True}
                for p in protected
                if p
            ],
            "writable": [
                {
                    "path": w,
                    "owner": user,
                    "mode": writable_mode,
                    "recursive": True,
                    "create": True,
                }
                for w in writable
                if w
            ],
        },
    }


def overlaps(protected: list[str], writable: list[str]) -> list[tuple[str, str]]:
    """Path pairs where one contains the other. Empty means the two lists are disjoint."""
    bad = []
    for w in writable:
        for p in protected:
            wn, pn = os.path.normpath(w), os.path.normpath(p)
            if wn == pn or wn.startswith(pn + os.sep) or pn.startswith(wn + os.sep):
                bad.append((w, p))
    return bad
