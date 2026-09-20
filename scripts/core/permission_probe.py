#!/usr/bin/env python3
"""One permission boundary, one probe protocol, one verdict — for every platform.

``permission_setup.py`` applies and ATTESTS path rules. This module owns everything that
surrounded it
and had been written four times: which paths each platform protects, creating the principal, handing
over the build cache, planting stand-ins, probing as the agent, and deciding the verdict.

Four copies produced four different versions of the same three bugs, each caught by a canary:

* a probe against an ABSENT path fails with ENOENT and proves nothing — counted as a denial on linux
  until a run where nothing was locked down still printed five of them.
* ``runuser``/a logon also fails when the agent cannot be assumed at all, which is indistinguishable
  from a denial — three windows canaries reported both protected roots "denied" while the
  boundary was
  never established. Hence the POSITIVE CONTROL: prove the agent can be assumed before believing
  anything it is refused.
* a failed attestation followed by probes reports the image's defaults as if they were a boundary.

So those rules live here once. What stays per-platform is only what genuinely differs: the PATH
VALUES
(each layout names its own directories) and the TRANSPORT (in-pod, pushed to a VM over SSH, run
on the
VM, or local to a container). Everything between is this module.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import pathlib
import shlex
import shutil
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from core import permission_spec as _spec  # noqa: E402

STAND_IN = ".rb_setup_probe"
STAND_IN_TEXT = "reference stand-in"

# Per-platform facts. `cache` is (env var to export, [candidate directories]) or None: the location
# belongs to the image, so it is DISCOVERED, and it is RELOCATED into the agent's home rather than
# chowned in place — a cache under /root stays unreachable after a perfect chown because /root
# is 0700.
PLATFORMS: dict[str, dict] = {
    "android": {
        "agent_user": "rbagent",
        "cache": (
            "GRADLE_USER_HOME",
            ["GRADLE_USER_HOME", "~root/.gradle", "/root/.gradle"],
        ),
    },
    # Linux recreation is launched with ``su -m user``.  The standalone setup stage must
    # attest that same principal; probing a separately-created ``rbagent`` account proves a
    # boundary the release agent never crosses.
    "linux": {"agent_user": "user", "cache": None},
    # devagent, not rbagent: macOS creates that standard-user principal
    # (prepare_sandbox.sh) and runs the recreation agent as it.
    "macos": {"agent_user": "devagent", "cache": None},
    "windows": {"agent_user": "rbagent", "cache": None},
    # This lane ALREADY demotes, to `agent`, via setpriv. Attesting anything else measures a
    # principal
    # production never uses — and chowning the workspace to it would break the real agent.
    "web": {
        "agent_user": "agent",
        "cache": ("NPM_CONFIG_CACHE", ["NPM_CONFIG_CACHE", "~root/.npm", "/root/.npm"]),
    },
}


def agent_user(platform: str) -> str:
    env = f"RB_{platform.upper()}_AGENT_USER"
    return os.environ.get(env) or PLATFORMS[platform]["agent_user"]


def _as_agent(user: str, argv: list[str]) -> int:
    """Run argv as the agent. POSIX only; windows attests through its own logon (see
    permission_setup._run_windows_as), so it never reaches here."""
    return subprocess.run(
        ["runuser", "-u", user, "--", *argv], capture_output=True, text=True
    ).returncode


def plant_stand_ins(protected: list[str]) -> list[str]:
    """attest refuses to read an absent path as "access denied", and rightly so. Without a file in
    each protected root a standalone setup run attests a boundary around empty directories.
    """
    planted = []
    for path in protected:
        root = pathlib.Path(path)
        # A platform may protect a sensitive file at the task root as well as directories.  The
        # file itself is already a live probe target; trying to mkdir it turns a valid boundary
        # into FileExistsError before setup can run.
        if root.exists() and not root.is_dir():
            planted.append(str(root))
            continue
        root.mkdir(parents=True, exist_ok=True)
        # Existing runtime trees already provide a live directory probe.  Do not inject a file into
        # an application bundle (notably a signed macOS .app) merely to attest its root.
        if any(root.iterdir()):
            continue
        marker = root / STAND_IN
        if not marker.exists():
            marker.write_text(STAND_IN_TEXT)
        planted.append(str(marker))
    return planted


def make_reachable(
    writable: list[str], *, user: str = "", windows: bool = False
) -> None:
    """open() needs +x on EVERY ancestor. Without this the agent cannot REACH its own workspace
    and the
    write probe fails for a reason that looks nothing like a permission boundary — it read as "not
    writable" on linux and as a red `writable:` check on windows.

    Traversal only, never +r: the agent reaches its directory by name and still cannot list the task
    directory, so the protected roots beside it stay opaque."""
    for path in writable:
        node = pathlib.Path(path).resolve()
        while node != node.parent:
            if windows:
                # chmod is very nearly a no-op on windows -- POSIX mode bits do not drive its ACLs.
                # (RX) is the traverse-only grant, and it must name the agent because there is no
                # "other" class to widen. Deleting the old windows stage nearly lost this: its
                # writable
                # check had failed for exactly this reason until icacls was added.
                subprocess.run(
                    ["icacls", str(node), "/grant", f"{user}:(RX)"],
                    capture_output=True,
                    text=True,
                )
            else:
                try:
                    node.chmod(node.stat().st_mode | 0o011)
                except OSError:
                    break
            node = node.parent


def probe(
    platform: str,
    protected: list[str],
    writable: list[str],
    *,
    user: str = "",
    cache_dir: str = "",
) -> tuple[bool, list[str]]:
    """Probe the boundary as the agent. Returns (ok, human-readable lines).

    POSIX only -- it works through ``runuser``. Windows never reaches here: its attestation already
    runs every check through the agent's token, so main() verifies the LOGON instead and lets the
    attestation be the probe.
    """
    user = user or agent_user(platform)
    lines: list[str] = []
    if shutil.which("runuser") is None:
        return False, ["no runuser: the boundary cannot be verified as the agent"]

    # POSITIVE CONTROL first. Every "denied" below is only evidence if the agent can be assumed
    # at all.
    if _as_agent(user, ["true"]) != 0:
        return False, [
            f"CANNOT RUN AS {user} -- every denial below would be meaningless, "
            "so none were attempted"
        ]
    lines.append(f"control : runuser -u {user} works")

    ok = True
    exercised: set[str] = set()
    for root in protected:
        for target in (f"{root}/{STAND_IN}", root):
            if not pathlib.Path(target).exists():
                lines.append(f"skipped : {target} (absent, nothing to deny)")
                continue
            argv = (
                ["ls", "-la", target]
                if pathlib.Path(target).is_dir()
                else ["cat", target]
            )
            if _as_agent(user, argv) == 0:
                lines.append(f"DENY-EXPECTED but SUCCEEDED: {target}")
                ok = False
            else:
                lines.append(f"denied  : {target}")
                exercised.add(root)

    # Refuse to call a boundary clean when no live probe exercised one of its roots.
    for root in protected:
        if root not in exercised:
            lines.append(
                f"NO LIVE PROBE exercised {root} — cannot claim the boundary holds"
            )
            ok = False

    for path in writable:
        marker = pathlib.Path(path) / ".rb_write_probe"
        if _as_agent(user, ["touch", str(marker)]) == 0:
            lines.append(f"writable: {path}")
            marker.unlink(missing_ok=True)
        else:
            lines.append(f"AGENT CANNOT WRITE ITS OWN WORKSPACE: {path}")
            ok = False

    if cache_dir:
        if (
            _as_agent(user, ["test", "-r", cache_dir]) == 0
            and _as_agent(user, ["test", "-x", cache_dir]) == 0
        ):
            lines.append(f"cache readable by the agent: {cache_dir}")
        else:
            lines.append(
                f"CACHE NOT READABLE BY THE AGENT: {cache_dir} (cold build ahead)"
            )
            ok = False
    return ok, lines


def prepare(platform: str, user: str = "") -> dict:
    """Create the principal and hand over the build cache. Reports what it did."""
    user = user or agent_user(platform)
    out: dict = {"platform": platform, "user": user}
    if platform == "windows":
        out["principal"] = _spec.ensure_windows_agent_user(user)
    else:
        out["principal"] = _spec.ensure_agent_user(user)
    spec_cache = PLATFORMS[platform]["cache"]
    if spec_cache:
        env_name, candidates = spec_cache
        resolved = [
            os.environ.get(c, "") if c.isupper() else os.path.expanduser(c)
            for c in candidates
        ]
        out["cache"] = _spec.hand_over_cache(user, resolved, env_name.lower())
        out["cache_env"] = env_name
    return out


def env_exports(platform: str, prepared: dict) -> list[str]:
    """What the agent must be told, or it silently starts a cold cache in its fresh HOME."""
    exports = [f"HOME=/home/{prepared['user']}"]
    cache = (prepared.get("cache") or {}).get("env_value")
    if cache and prepared.get("cache_env"):
        exports.append(f"{prepared['cache_env']}={cache}")
    return exports


# ── transport ───────────────────────────────────────────────────────────────────────────────────
#
# There are exactly TWO shapes, and the line is not pod-vs-VM. What decides it is whether the
# probe is
# already ON the machine that owns the paths:
#
#   local   web (container), android (pod), and WINDOWS -- scripts/windows/worker.py has 34 local
#           path operations and no ssh, i.e. it already executes on the VM.
# remote linux and macOS -- the pipeline runs in the pod while the paths live on a VM, so the
# probe
#           has to be shipped there. Both already speak core.vmclient.VmClient.
#
# Only `remote` needs code, and only because the modules must travel. Neither knows anything about a
# platform beyond its paths.

CLI = "core/permission_probe.py"
_SHIP = (
    "core/permission_probe.py",
    "core/permission_setup.py",
    "core/permission_spec.py",
    "core/runtime_assets.py",
    "core/assets/windows_impersonation.ps1",
)


def ship_modules(client, remote_dir: str) -> None:
    """Copy the self-contained probe implementation to a VM without executing it."""
    sep = client.remote_sep
    remote_dir = remote_dir.replace("/", sep) if sep != "/" else remote_dir
    here = pathlib.Path(__file__).resolve().parents[1]
    for rel in _SHIP:
        client.upload_text(
            (here / rel).read_text(),
            f"{remote_dir}{sep}{rel.replace('/', sep)}",
        )


def _cli_args(
    platform: str, run_id: str, protected, writable, work: str, user: str = ""
) -> list[str]:
    args = ["--platform", platform, "--run-id", run_id]
    for path in protected:
        args += ["--protected", path]
    for path in writable:
        args += ["--writable", path]
    args += [
        "--spec-out",
        f"{work}/spec.json",
        "--report",
        f"{work}/report.json",
        "--env-out",
        f"{work}/permission_env.sh",
    ]
    if user:
        args += ["--user", user]
    return args


def run_local(
    platform: str, run_id: str, protected, writable, *, work: str = "", user: str = ""
) -> tuple[bool, str]:
    """The probe runs where the paths already are. web, android and windows all take this route."""
    work = work or str(
        pathlib.Path(os.environ.get("TMPDIR") or "/tmp") / "rb_permission"
    )
    pathlib.Path(work).mkdir(parents=True, exist_ok=True)
    cli = _cli_args(platform, run_id, protected, writable, work, user)
    if platform == "windows":
        # Windows account setup generates a fresh password in RB_AGENT_PASSWORD.  Recreation must
        # use that exact secret to start the agent, so run the shared CLI in-process and retain the
        # environment mutation.  The report/transcript still never contains the password.
        stdout, stderr = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
                rc = main(cli)
        except SystemExit as exc:
            rc = int(exc.code or 0)
        transcript = stdout.getvalue()
        if stderr.getvalue().strip():
            transcript += "\n" + stderr.getvalue()
        return rc == 0, transcript.strip()

    proc = subprocess.run(
        [sys.executable, str(pathlib.Path(__file__).resolve()), *cli],
        capture_output=True,
        text=True,
    )
    transcript = (
        proc.stdout + ("\n" + proc.stderr if proc.stderr.strip() else "")
    ).strip()
    return proc.returncode == 0, transcript


POSIX_PLATFORMS = ("linux", "macos")


def _posix_script(remote_dir: str, args: str, sudo_password: str) -> str:
    """bash, and it must ELEVATE: the script arrives as the SSH user and creating a principal needs
    root. `sudo -n` alone is not enough -- that user is in the sudo GROUP but sudo still wants a
    password, which cost a canary."""
    script = f"""set -uo pipefail
if [ "$(id -u)" -ne 0 ]; then
    if sudo -n true 2>/dev/null; then
        exec sudo -E bash "$0" "$@"
    elif [ -n "${{RB_SUDO_PASSWORD:-}}" ]; then
        # NOT `| exec sudo`: a pipeline's last stage is a SUBSHELL, so exec replaces the child
        # and the parent carries on unprivileged. That ran one canary's script twice -- once
        # clean as root, once failing as the SSH user -- and the verdict read the failure.
        echo "$RB_SUDO_PASSWORD" | sudo -S -E bash "$0" "$@"
        exit $?
    else
        echo "SETUP FAILED: need root to create the principal and no sudo access"
        exit 2
    fi
fi
PYTHONPATH={remote_dir} python3 {remote_dir}/{CLI} {args}
"""
    if sudo_password:
        script = f"export RB_SUDO_PASSWORD={shlex.quote(sudo_password)}\n" + script
    return script


def _windows_command(remote_dir: str, args: str) -> str:
    """PowerShell, and deliberately NO elevation: the SSH user on these guests is already an
    administrator -- winsetup18 created a local account, ran secedit and reset icacls owners through
    it, and attestation resolved its trusted principal to `administrator`.

    2>&1 so a traceback lands in the transcript the POD judges rather than only on remote stderr,
    which the ssh helper truncates to 200 chars; that truncation once left a failure undiagnosable.
    """
    return (
        'powershell -NoProfile -Command "'
        f"$env:PYTHONPATH='{remote_dir}'; "
        f'python {remote_dir}/{CLI} {args} 2>&1"'
    )


def run_remote(
    client,
    platform: str,
    run_id: str,
    protected,
    writable,
    *,
    remote_dir: str = "",
    user: str = "",
    sudo_password: str = "",
    timeout: int = 600,
) -> tuple[bool, str]:
    """Ship the probe to the machine that owns the paths, run it there, JUDGE HERE.

    ``client`` is a core.vmclient.VmClient. The probe must execute on the VM -- that is where the
    paths are -- but the verdict is formed in this process from the transcript. That asymmetry is
    the
    point: letting the machine under attestation also decide whether it passed is the weaker
    arrangement, and it is what made a windows failure unreadable (`exit=1` with no way to tell
    which
    route had answered).

    Two dialects, one concept. POSIX guests get bash with sudo elevation and the detached
    submit/poll
    model; a Windows guest gets PowerShell through run(), because submit/poll are POSIX by
    construction (`mkdir -p`, a heredoc, `timeout -k`) and VmClient refuses them on a Windows guest.
    """
    is_posix = platform in POSIX_PLATFORMS
    remote_dir = remote_dir or (
        "/tmp/rb_permission" if is_posix else r"C:\rb_permission"
    )
    # VmClient deliberately records the guest's path separator.  In particular its SFTP mkdir-p
    # implementation splits Windows paths on ``\\``; feeding it ``C:/...`` leaves the parent
    # uncreated and the first upload fails with ENOENT before the probe can run.
    sep = client.remote_sep
    if not is_posix:
        remote_dir = remote_dir.replace("/", sep)
    ship_modules(client, remote_dir)

    cli = _cli_args(platform, run_id, protected, writable, remote_dir, user)
    args = " ".join(shlex.quote(a) for a in cli) if is_posix else " ".join(cli)

    if not is_posix:
        res = client.run(
            _windows_command(remote_dir, args), timeout=timeout, check=False
        )
        out = (res.stdout or "") + (
            ("\n" + res.stderr) if (res.stderr or "").strip() else ""
        )
        return ("SETUP OK" in out and "SETUP FAILED" not in out), out.strip()

    invoke = client.submit(
        _posix_script(remote_dir, args, sudo_password), timeout=timeout
    )
    deadline = time.time() + timeout + 120
    while time.time() < deadline:
        # poll returns a dict while the script is STILL RUNNING, so breaking on the first non-None
        # result reads an empty output -- the first linux canary reported "(no output from the VM)".
        res = client.poll(invoke)
        if res and res.get("status") in ("Success", "Failed", "Timeout"):
            out = res.get("output", "") or ""
            return ("SETUP OK" in out and "SETUP FAILED" not in out), out
        time.sleep(2)
    return False, "the probe never reached a terminal status on the remote host"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--platform", required=True, choices=sorted(PLATFORMS))
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--protected", action="append", default=[], required=True)
    ap.add_argument("--writable", action="append", default=[], required=True)
    ap.add_argument("--writable-mode", choices=("0700", "0755"), default="0700")
    ap.add_argument("--user", default="")
    ap.add_argument("--spec-out", required=True)
    ap.add_argument("--report", required=True)
    ap.add_argument("--env-out")
    args = ap.parse_args(argv)

    user = args.user or agent_user(args.platform)
    bad = _spec.overlaps(args.protected, args.writable)
    if bad:
        print(f"ERROR: protected/writable overlap: {bad}", file=sys.stderr)
        return 64

    prepared = prepare(args.platform, user)
    print(json.dumps(prepared, indent=2))

    plant_stand_ins(args.protected)
    pathlib.Path(args.writable[0]).mkdir(parents=True, exist_ok=True)
    spec = _spec.assemble(
        args.platform,
        args.run_id,
        protected=args.protected,
        writable=args.writable,
        writable_mode=args.writable_mode,
        user=user,
        trusted=(
            (os.environ.get("USERNAME") or "Administrator")
            if args.platform == "windows"
            else "root"
        ),
    )
    pathlib.Path(args.spec_out).write_text(json.dumps(spec, indent=2))
    # After prepare(), never before: icacls resolves the account NAME, and granting to a principal
    # that does not exist yet fails with 1332 ("no mapping between account names and security IDs").
    make_reachable(args.writable, user=user, windows=args.platform == "windows")

    attest = subprocess.run(
        [
            sys.executable,
            str(pathlib.Path(__file__).with_name("permission_setup.py")),
            "setup",
            "--spec",
            args.spec_out,
            "--report",
            args.report,
        ],
        capture_output=True,
        text=True,
    )
    print(attest.stdout.strip())
    if attest.stderr.strip():
        print(attest.stderr.strip(), file=sys.stderr)
    print(f"attest_rc={attest.returncode}")
    if attest.returncode != 0:
        # With nothing applied, any probe describes the image's defaults rather than a boundary.
        print("SETUP FAILED: attestation did not pass; probes skipped")
        return 2

    if args.platform == "windows":
        # No runuser here, and none needed: permission_setup's windows attestation already ran every
        # check through the agent's own token (LogonUser + ImpersonateLoggedOnUser), so IT is the
        # effective-user probe. What that leaves missing is the positive control -- a logon that
        # cannot be made fails identically to one that was refused, which made three canaries report
        # both protected roots "denied" while no boundary existed. So verify the logon explicitly,
        # through the same runner the attestation used.
        from core import permission_setup as _ps

        control = _ps._run_windows_as(_ps.load_spec(args.spec_out), "exit 0")
        ok = control.returncode == 0
        lines = [
            f"logon control: exit={control.returncode} {control.stderr.strip()[:120]}"
        ]
        if not ok:
            lines.append(
                f"CANNOT RUN AS {user} -- the attestation's checks all run through this logon, "
                "so its denials would be meaningless"
            )
    elif args.platform == "macos":
        # macOS has no runuser. permission_setup's POSIX attestation already executes
        # every protected/writable access check after setgroups/setgid/setuid, and a
        # failed demotion makes the report fail. Do not follow that valid attestation
        # with the Linux-only runuser probe and turn it into a false negative.
        ok = True
        lines = [f"control : effective-user attestation as {user} passed"]
    else:
        ok, lines = probe(
            args.platform,
            args.protected,
            args.writable,
            user=user,
            cache_dir=(prepared.get("cache") or {}).get("env_value", ""),
        )
    print("=== PROBES ===")
    for line in lines:
        print(f"  {line}")
    if args.env_out:
        pathlib.Path(args.env_out).write_text(
            "\n".join(f"export {e}" for e in env_exports(args.platform, prepared))
            + "\n"
        )
    print(
        "SETUP OK"
        if ok
        else "SETUP FAILED: the boundary attested but the probes disagree"
    )
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
