"""Shared SSH/SCP invocation for the sshpass-over-OpenSSH platforms (linux + macOS).

The three desktop platforms each drive a VM from the deployment platform container, and each had grown its
own SSH layer. Windows genuinely needs a different transport — paramiko, for SFTP and for
Windows paths over 260 chars — but linux and macOS were both shelling out to
``sshpass``/``ssh`` with independently maintained option strings, which had drifted in two
ways that matter:

* **keepalives**: three different policies. linux's ECS exec layer used 60/5, macOS 30/3,
  and the linux deployment platform controller none at all — so a long silent stage there had nothing
  keeping the channel alive. Long-running desktop stages losing their SSH channel is a
  known failure mode, so the shared set standardises on the most tolerant of the three
  (60 x 5 = 5 minutes of silence survived) for everyone.
* **the password**: linux passed it as ``sshpass -p <password>``, i.e. on the argv, where
  any process listing on the deployment platform container shows it. macOS already used ``sshpass -e`` and
  said why. The shared builders never accept a password at all — the caller puts it in the
  child env via :func:`env_with_password`, so it cannot reach the process table.

Only argv/env construction lives here: pure, dependency-free, unit-testable. Process
handling (streaming, timeouts, retry) stays with each caller, which is where the platforms
legitimately differ.
"""

from __future__ import annotations

import os

# Shared behaviour for both platforms. ConnectTimeout stays a parameter because it is a
# real policy difference — linux uses a short one so a readiness probe fails fast and
# retries, macOS a longer one for a slower-booting VM.
BASE_OPTS: tuple[str, ...] = (
    "-o",
    "StrictHostKeyChecking=no",
    "-o",
    "UserKnownHostsFile=/dev/null",
    "-o",
    "LogLevel=ERROR",
    "-o",
    "ServerAliveInterval=60",
    "-o",
    "ServerAliveCountMax=5",
)


def opts_argv(connect_timeout: int = 30) -> list[str]:
    """The shared ``-o`` list as argv items."""
    return list(BASE_OPTS) + ["-o", f"ConnectTimeout={int(connect_timeout)}"]


def opts_string(connect_timeout: int = 30) -> str:
    """The same options as one shell-quotable string, for callers that build a shell
    command line rather than an argv list (macOS's ssh/scp helpers)."""
    return " ".join(opts_argv(connect_timeout))


def ssh_argv(
    host: str,
    user: str,
    port: int = 22,
    *,
    connect_timeout: int = 30,
    with_password: bool = True,
    key_path: str = "",
) -> list[str]:
    """argv prefix for a remote command: ``[sshpass -e] ssh <opts> user@host``.

    ``with_password`` only decides whether to wrap in ``sshpass -e``; the password itself
    travels in the environment (see :func:`env_with_password`), never here.
    """
    argv: list[str] = ["sshpass", "-e"] if with_password else []
    argv += ["ssh", "-p", str(int(port))]
    argv += opts_argv(connect_timeout)
    if key_path:
        argv += ["-i", key_path]
    argv.append(f"{user}@{host}")
    return argv


def scp_argv(
    host: str,
    user: str,
    port: int = 22,
    *,
    connect_timeout: int = 30,
    with_password: bool = True,
    recursive: bool = False,
) -> list[str]:
    """argv prefix for scp. Source/destination are appended by the caller, because the
    direction (to-VM vs from-VM) decides which side carries ``user@host:``."""
    argv: list[str] = ["sshpass", "-e"] if with_password else []
    argv += ["scp", "-P", str(int(port))]
    if recursive:
        argv.append("-r")
    argv += opts_argv(connect_timeout)
    return argv


def env_with_password(base_env: dict | None, password: str) -> dict:
    """Child env carrying the password in ``SSHPASS`` for ``sshpass -e``.

    An empty password leaves the variable unset rather than exporting an empty one, so
    key-based auth still works.
    """
    env = dict(base_env if base_env is not None else os.environ)
    if password:
        env["SSHPASS"] = password
    return env
