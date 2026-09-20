#!/usr/bin/env python3
"""One Paramiko/SFTP transfer surface for platforms that drive a VM.

Before this, "how VM files are transferred" was re-decided per platform: macOS shelled out to
``scp`` while Windows used Paramiko/SFTP. This is the shared Paramiko implementation used by all
desktop workers. It also owns command execution where the worker does not require an OpenSSH
process for streaming or port forwarding. ``LocalClient`` remains an explicit development/test
utility; no release adapter selects it.

TWO PROPERTIES ARE LOAD-BEARING. Read before changing anything here.

1. **Detached execution, not a long-lived channel.** ``submit`` writes the script to
   ``/tmp/rb_invocations/<id>``, launches it detached under ``timeout -k``, and returns immediately;
   ``poll`` reads ``exit_code``/``output`` later. That is *why* a 20h recreation survives a dropped
   connection -- the script keeps running and the next poll picks it up. Paramiko replaced the
   command channel, never this execution model.

2. **Reconnect is detect-before-send, not retry-after-failure.** A dead connection is noticed by
   ``transport.is_active()`` BEFORE a command goes out, so reconnecting cannot double-run anything.
   Retrying *after* a failure is at-least-once: if the channel dropped after the remote started the
   command, a retry runs it twice -- and two ``bash script.sh`` writing one ``output`` file corrupt
   each other. So retry-after-failure is opt-in per call (``idempotent=True``) and the launcher does
   NOT opt in. Retrying it could duplicate a remote command and corrupt its output.
"""

from __future__ import annotations

import ntpath
import os
import posixpath
import select
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

INVOCATION_ROOT = "/tmp/rb_invocations"

# A 50-way desktop launch can briefly exhaust a worker node's ability to create another local
# thread even when this pod is well below its own pids/memory limits.  Paramiko starts a transport
# thread before any SSH command is sent, so retrying this particular *connect-time* failure is
# still at-most-once.  Keep the scope deliberately narrow: authentication, routing and ordinary
# SSH failures must surface immediately rather than being hidden behind another retry policy.
_CONNECT_RESOURCE_ATTEMPTS = 6
_CONNECT_RESOURCE_RETRY_DELAY_SEC = 5


def _is_transient_local_resource_error(exc: BaseException) -> bool:
    """Whether ``exc`` is the local EAGAIN seen while Paramiko starts its transport thread."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        message = str(current).lower()
        if (
            "can't start new thread" in message
            or "resource temporarily unavailable" in message
        ):
            return True
        current = current.__cause__ or current.__context__
    return False


# The remote status program is a file so its source can be linted and tested as
# ordinary Python.  It is streamed over stdin because a poll must remain a pure
# read and must not depend on a separately installed remote helper.
_SSH_STATUS_PY = (
    Path(__file__).with_name("assets") / "vm_invocation_status.py"
).read_text(encoding="utf-8")


@dataclass(frozen=True)
class Result:
    rc: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.rc == 0


class VmClient:
    """SSH + SFTP to one VM.

    ``remote_sep`` is not cosmetic: the recursive transfers join remote paths with it, and a Windows
    guest needs ``\\``. Getting it wrong silently creates files with backslashes in their names on a
    POSIX guest instead of directories.
    """

    def __init__(
        self,
        host: str,
        user: str = "user",
        password: str = "",
        port: int = 22,
        key_path: str = "",
        connect_timeout: int = 10,
        remote_sep: str = "/",
    ) -> None:
        if not host:
            raise ValueError("VmClient requires a host (RB_SSH_HOST or --host)")
        self.host = host
        self.user = user
        self.password = password
        self.port = int(port)
        self.key_path = key_path
        self.connect_timeout = int(connect_timeout)
        self.remote_sep = remote_sep
        self._ssh = None
        self._keepalive_session = False
        self._keepalive_channel = None

    @classmethod
    def from_env(cls, **overrides) -> "VmClient":
        """Same env names the ssh-CLI client used, so --host and the deployment platform controllers keep working."""
        kwargs = dict(
            host=os.environ.get("RB_SSH_HOST", ""),
            user=os.environ.get("RB_SSH_USER", "user"),
            password=os.environ.get("RB_SSH_PASSWORD", ""),
            port=int(os.environ.get("RB_SSH_PORT", "22")),
            key_path=os.environ.get("RB_SSH_KEY_PATH", ""),
            connect_timeout=int(os.environ.get("RB_SSH_CONNECT_TIMEOUT", "10")),
        )
        kwargs.update(overrides)
        return cls(**kwargs)

    # --- connection -------------------------------------------------------------------------

    def _connect(self):
        import paramiko

        kwargs: dict = {
            "hostname": self.host,
            "port": self.port,
            "username": self.user,
            "timeout": self.connect_timeout,
            "banner_timeout": max(30, self.connect_timeout),
            "auth_timeout": max(30, self.connect_timeout),
        }
        if self.key_path:
            kwargs["key_filename"] = self.key_path
        elif self.password:
            kwargs["password"] = self.password
            kwargs["look_for_keys"] = False
        # else: no key path and no password -- let paramiko find the agent / default keys. macOS
        # authenticates exactly this way, and forcing look_for_keys=False here would break it.
        for attempt in range(1, _CONNECT_RESOURCE_ATTEMPTS + 1):
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            try:
                ssh.connect(**kwargs)
            except Exception as exc:
                try:
                    ssh.close()
                except Exception:
                    pass
                if (
                    not _is_transient_local_resource_error(exc)
                    or attempt == _CONNECT_RESOURCE_ATTEMPTS
                ):
                    raise
                print(
                    "[vmclient] local thread/process capacity temporarily unavailable; "
                    f"retrying SSH connect ({attempt}/{_CONNECT_RESOURCE_ATTEMPTS}) in "
                    f"{_CONNECT_RESOURCE_RETRY_DELAY_SEC}s",
                    file=sys.stderr,
                    flush=True,
                )
                time.sleep(_CONNECT_RESOURCE_RETRY_DELAY_SEC)
                continue
            transport = ssh.get_transport()
            if transport is not None:
                # Without this a poll loop that idles between polls gets its connection reaped by a
                # firewall or the sshd and only finds out on the next send.
                transport.set_keepalive(30)
            self._ssh = ssh
            if self._keepalive_session:
                self._open_keepalive_channel(ssh)
            return ssh
        raise AssertionError("unreachable")

    def _client(self):
        """Live client, reconnecting if the previous one died -- BEFORE anything is sent."""
        ssh = self._ssh
        if ssh is not None:
            transport = ssh.get_transport()
            if transport is not None and transport.is_active():
                return ssh
            self.close()
        return self._connect()

    def close(self) -> None:
        if self._keepalive_channel is not None:
            try:
                self._keepalive_channel.close()
            except Exception:
                pass
            self._keepalive_channel = None
        if self._ssh is not None:
            try:
                self._ssh.close()
            except Exception:
                pass
            self._ssh = None

    def _open_keepalive_channel(self, ssh) -> None:
        """Keep one idle session channel open on guests that reap channel-less transports."""
        channel = ssh.invoke_shell(width=80, height=24)
        channel.settimeout(0.0)
        self._keepalive_channel = channel

    def keep_session_open(self) -> None:
        """Anchor subsequent exec and SFTP channels to one persistent SSH transport."""
        self._keepalive_session = True
        ssh = self._client()
        if self._keepalive_channel is None or self._keepalive_channel.closed:
            self._open_keepalive_channel(ssh)

    @contextmanager
    def remote_forward(
        self,
        host: str,
        port: int,
        *,
        remote_host: str = "127.0.0.1",
        remote_port: int = 0,
    ):
        """Expose a controller-reachable TCP service on the SSH guest."""
        if not host:
            raise ValueError("remote_forward requires an upstream host")
        upstream_port = int(port)
        if not 1 <= upstream_port <= 65535:
            raise ValueError(f"invalid upstream port: {port!r}")
        requested_port = int(remote_port)
        if not 0 <= requested_port <= 65535:
            raise ValueError(f"invalid remote port: {remote_port!r}")

        transport = self._client().get_transport()
        if transport is None or not transport.is_active():
            raise RuntimeError("SSH transport is not active")

        stopping = threading.Event()
        lock = threading.Lock()
        streams: set = set()
        workers: set[threading.Thread] = set()

        def bridge(channel) -> None:
            upstream = None
            current = threading.current_thread()
            try:
                upstream = socket.create_connection(
                    (host, upstream_port), timeout=self.connect_timeout
                )
                upstream.settimeout(None)
                with lock:
                    if stopping.is_set():
                        return
                    streams.update((channel, upstream))
                while not stopping.is_set():
                    readable, _, _ = select.select((channel, upstream), (), (), 0.5)
                    if channel in readable:
                        data = channel.recv(65536)
                        if not data:
                            break
                        upstream.sendall(data)
                    if upstream in readable:
                        data = upstream.recv(65536)
                        if not data:
                            break
                        channel.sendall(data)
            except (OSError, ValueError) as exc:
                if not stopping.is_set():
                    print(
                        "[vmclient] remote-forward upstream connection failed "
                        f"for {host}:{upstream_port}: {type(exc).__name__}",
                        file=sys.stderr,
                        flush=True,
                    )
            finally:
                with lock:
                    streams.discard(channel)
                    if upstream is not None:
                        streams.discard(upstream)
                    workers.discard(current)
                for stream in (channel, upstream):
                    if stream is not None:
                        try:
                            stream.close()
                        except Exception:
                            pass

        def handle(channel, _origin, _server) -> None:
            if stopping.is_set():
                channel.close()
                return
            worker = threading.Thread(
                target=bridge,
                args=(channel,),
                name="rb-ssh-remote-forward",
                daemon=True,
            )
            with lock:
                workers.add(worker)
            try:
                worker.start()
            except RuntimeError as exc:
                with lock:
                    workers.discard(worker)
                channel.close()
                print(
                    "[vmclient] could not start remote-forward bridge: "
                    f"{type(exc).__name__}",
                    file=sys.stderr,
                    flush=True,
                )

        allocated_port = transport.request_port_forward(
            remote_host, requested_port, handler=handle
        )
        try:
            yield int(allocated_port)
        finally:
            stopping.set()
            try:
                transport.cancel_port_forward(remote_host, int(allocated_port))
            except Exception:
                pass
            with lock:
                active_streams = tuple(streams)
                active_workers = tuple(workers)
            for stream in active_streams:
                try:
                    stream.close()
                except Exception:
                    pass
            for worker in active_workers:
                worker.join(timeout=2)

    # --- commands ---------------------------------------------------------------------------

    def _exec(
        self,
        command: str,
        *,
        timeout: int = 120,
        stdin_text: str | None = None,
        idempotent: bool = False,
    ) -> Result:
        try:
            return self._exec_once(command, timeout=timeout, stdin_text=stdin_text)
        except Exception:
            if not idempotent:
                raise
            # Safe to re-send: the caller declared this command has no side effect worth
            # duplicating (a pure read, or a whole-file overwrite).
            self.close()
            return self._exec_once(command, timeout=timeout, stdin_text=stdin_text)

    def _exec_once(
        self, command: str, *, timeout: int, stdin_text: str | None
    ) -> Result:
        ssh = self._client()
        stdin, stdout, stderr = ssh.exec_command(command, timeout=timeout)
        if stdin_text is not None:
            stdin.write(stdin_text)
            stdin.flush()
        stdin.channel.shutdown_write()
        out = stdout.read().decode(errors="replace")
        err = stderr.read().decode(errors="replace")
        rc = stdout.channel.recv_exit_status()
        return Result(rc=rc, stdout=out, stderr=err)

    def run(
        self,
        command: str,
        timeout: int = 120,
        check: bool = True,
        *,
        stdin_text: str | None = None,
        idempotent: bool = False,
    ) -> Result:
        """One short command, synchronously. Long work belongs in submit/poll."""
        res = self._exec(
            command, timeout=timeout, stdin_text=stdin_text, idempotent=idempotent
        )
        if check and res.rc != 0:
            detail = (res.stderr or res.stdout).strip()
            raise RuntimeError(
                f"ssh command failed on {self.host} exit={res.rc}: {detail[:500]}"
            )
        return res

    # --- the detached contract --------------------------------------------------------------

    def submit(self, script: str, timeout: int = 3600) -> str:
        """Launch ``script`` detached; return an invoke_id to poll.

        The remote recipe is byte-identical to the ssh-CLI/ECS one it replaces. The launcher is NOT
        marked idempotent: a retry would start a second copy writing the same output file.
        """
        self._require_posix("submit")
        invoke_id = uuid.uuid4().hex
        remote_dir = f"{INVOCATION_ROOT}/{invoke_id}"
        script_path = f"{remote_dir}/script.sh"
        self.run(f"mkdir -p {_q(remote_dir)}", timeout=30, idempotent=True)
        self.upload_text(script, script_path)
        command_timeout = max(1, int(timeout))
        launcher = (
            f"cd {_q(remote_dir)} || exit 1\n"
            "rm -f exit_code output pid\n"
            f"(timeout -k 30s {command_timeout}s bash script.sh > output 2>&1; "
            "rc=$?; printf '%s\\n' \"$rc\" > exit_code) "
            "</dev/null >/dev/null 2>&1 &\n"
            "echo $! > pid"
        )
        self.run(launcher, timeout=30)
        return invoke_id

    def poll(self, invoke_id: str) -> dict | None:
        """Status of a submitted script: {status, exit_code, output}. A pure read, so retryable."""
        self._require_posix("poll")
        remote_dir = f"{INVOCATION_ROOT}/{invoke_id}"
        command = f"RB_INVOCATION_DIR={_q(remote_dir)} python3 -"
        try:
            res = self._exec(
                command,
                timeout=60,
                stdin_text=_SSH_STATUS_PY,
                idempotent=True,
            )
        except Exception as exc:
            return {
                "status": "Failed",
                "exit_code": 255,
                "output": f"poll failed: {exc}",
            }
        if res.rc != 0:
            return {
                "status": "Failed",
                "exit_code": res.rc,
                "output": (res.stderr or res.stdout).strip(),
            }
        try:
            import json

            return json.loads(res.stdout)
        except Exception:
            return {
                "status": "Failed",
                "exit_code": 255,
                "output": res.stdout[-4000:],
            }

    def _require_posix(self, what: str) -> None:
        """submit/poll are POSIX by construction -- `mkdir -p`, `bash`, a heredoc, `timeout -k`.

        Only linux and macOS use them; windows drives its VM synchronously through run(). The guard
        exists because the POSIX-shell-on-a-Windows-guest mistake already cost a canary: upload_text
        shelled out to `mkdir -p` and cmd answered "The filename, directory name, or volume label
        syntax is incorrect", which reads like a path bug rather than a wrong-shell bug.
        """
        if self.remote_sep != "/":
            raise RuntimeError(
                f"{what}() is POSIX-only (it runs mkdir -p / bash / a heredoc) but this client "
                f"targets a guest with remote_sep={self.remote_sep!r}. Use run() and the SFTP "
                "transfers for a Windows guest."
            )

    # --- file transfer ----------------------------------------------------------------------

    def _sftp(self):
        return self._client().open_sftp()

    def upload_text(self, content: str, remote_path: str) -> None:
        """Whole-file write over SFTP (the ssh-CLI client piped base64 through stdin, and the ECS
        one chunked it 8000 chars at a time with a sleep between chunks).

        NO SHELL. The first version created the parent with `mkdir -p`, which is POSIX: on a
        Windows guest cmd rejected it outright ("The filename, directory name, or volume label
        syntax is incorrect. Error occurred while processing: 'C:\rb_pipeline'"), and shlex.quote
        mangled the backslash path on top of that. SFTP creates directories the same way on every
        guest, so the shell never enters into it.
        """
        sftp = self._sftp()
        try:
            self._ensure_remote_dir(sftp, self._remote_parent(remote_path))
            with sftp.open(remote_path, "w") as fh:
                fh.write(content)
        finally:
            sftp.close()

    def _remote_parent(self, remote_path: str) -> str:
        # Windows SFTP callers use both C:/file and C:\\file. Use guest path
        # semantics, including bare relative filenames, regardless of the host OS.
        if self.remote_sep == "\\":
            return ntpath.dirname(remote_path).replace("/", "\\")
        return posixpath.dirname(remote_path)

    def _ensure_remote_dir(self, sftp, remote_dir: str) -> None:
        """mkdir -p, but over SFTP so it is guest-agnostic. Missing components are created in
        order; an existing one is left alone."""
        if not remote_dir:
            return
        parts = [p for p in remote_dir.split(self.remote_sep) if p]
        if not parts:
            return
        # keep a leading separator (POSIX absolute paths); a drive letter needs none
        prefix = self.remote_sep if remote_dir.startswith(self.remote_sep) else ""
        path = prefix
        for part in parts:
            path = (
                part
                if not path
                else f"{path.rstrip(self.remote_sep)}{self.remote_sep}{part}"
            )
            try:
                sftp.stat(path)
            except Exception:
                try:
                    sftp.mkdir(path)
                except Exception:
                    # a concurrent create, or a drive root that cannot be stat'd -- carry on and
                    # let the actual write report the real problem
                    pass

    def upload_file(
        self, local_path: str, remote_path: str, *, mode: int | None = None
    ) -> None:
        sftp = self._sftp()
        try:
            self._ensure_remote_dir(sftp, self._remote_parent(remote_path))
            if mode is not None:
                # chmod an empty destination before writing sensitive content;
                # sftp.put() then truncates the same file without widening it.
                with sftp.open(remote_path, "w"):
                    pass
                sftp.chmod(remote_path, mode)
            sftp.put(local_path, remote_path)
            if mode is not None:
                try:
                    sftp.chmod(remote_path, mode)
                except Exception:
                    try:
                        sftp.remove(remote_path)
                    except Exception:
                        pass
                    raise
        finally:
            sftp.close()

    def download_file(self, remote_path: str, local_path: str) -> None:
        """Download one file over the same SFTP transport used for directory trees."""
        os.makedirs(os.path.dirname(os.path.abspath(local_path)), exist_ok=True)
        sftp = self._sftp()
        try:
            sftp.get(remote_path, local_path)
        finally:
            sftp.close()

    def upload_dir(
        self, local_dir: str, remote_dir: str, exclude_dirs: set[str] | None = None
    ) -> int:
        if not os.path.isdir(local_dir):
            return 0
        sftp = self._sftp()
        try:
            return self._upload_recursive(sftp, local_dir, remote_dir, exclude_dirs)
        finally:
            sftp.close()

    def _upload_recursive(
        self, sftp, local: str, remote: str, exclude_dirs: set[str] | None
    ) -> int:
        self._ensure_remote_dir(sftp, remote)
        count = 0
        for entry in sorted(os.listdir(local)):
            if exclude_dirs and entry in exclude_dirs:
                continue
            lpath = os.path.join(local, entry)
            rpath = f"{remote}{self.remote_sep}{entry}"
            if os.path.isdir(lpath):
                count += self._upload_recursive(sftp, lpath, rpath, exclude_dirs)
            else:
                try:
                    sftp.put(lpath, rpath)
                    count += 1
                except Exception as exc:
                    print(
                        f"  WARN: failed to upload {lpath} -> {rpath}: {exc}",
                        file=sys.stderr,
                    )
        return count

    def download_dir(
        self,
        remote_dir: str,
        local_dir: str,
        exclude_patterns: set[str] | tuple[str, ...] | None = None,
    ) -> int:
        os.makedirs(local_dir, exist_ok=True)
        sftp = self._sftp()
        try:
            return self._download_recursive(
                sftp, remote_dir, local_dir, exclude_patterns
            )
        finally:
            sftp.close()

    def _download_recursive(
        self,
        sftp,
        remote: str,
        local: str,
        exclude_patterns: set[str] | tuple[str, ...] | None,
    ) -> int:
        os.makedirs(local, exist_ok=True)
        count = 0
        try:
            entries = sftp.listdir_attr(remote)
        except Exception as exc:
            print(f"  WARN: failed to list remote dir {remote}: {exc}", file=sys.stderr)
            return 0
        for entry in entries:
            if exclude_patterns and any(
                fnmatch(entry.filename, pattern) for pattern in exclude_patterns
            ):
                continue
            rpath = f"{remote}{self.remote_sep}{entry.filename}"
            lpath = os.path.join(local, entry.filename)
            mode = entry.st_mode or 0
            if stat.S_ISLNK(mode):
                try:
                    target = sftp.readlink(rpath)
                    if os.path.lexists(lpath):
                        if os.path.isdir(lpath) and not os.path.islink(lpath):
                            shutil.rmtree(lpath)
                        else:
                            os.unlink(lpath)
                    os.symlink(target, lpath)
                    count += 1
                except Exception as exc:
                    print(
                        f"  WARN: failed to download symlink {rpath} -> {lpath}: {exc}",
                        file=sys.stderr,
                    )
            elif stat.S_ISDIR(mode):
                count += self._download_recursive(sftp, rpath, lpath, exclude_patterns)
                self._apply_local_attrs(lpath, entry)
            else:
                try:
                    sftp.get(rpath, lpath)
                    self._apply_local_attrs(lpath, entry)
                    count += 1
                except Exception as exc:
                    print(
                        f"  WARN: failed to download {rpath} -> {lpath}: {exc}",
                        file=sys.stderr,
                    )
        return count

    @staticmethod
    def _apply_local_attrs(path: str, attrs) -> None:
        """Retain executable bits and timestamps when materializing VM artifacts."""
        mode = getattr(attrs, "st_mode", None)
        if mode:
            try:
                os.chmod(path, stat.S_IMODE(mode))
            except OSError:
                pass
        mtime = getattr(attrs, "st_mtime", None)
        atime = getattr(attrs, "st_atime", None) or mtime
        if mtime is not None:
            try:
                os.utime(path, (atime, mtime))
            except OSError:
                pass


class LocalClient(VmClient):
    """Explicit development/test implementation of the VM command surface."""

    def __init__(self) -> None:
        # Do not call VmClient.__init__: a local execution target has no SSH host.
        self.host = "local-pod"
        self.user = ""
        self.password = ""
        self.port = 0
        self.key_path = ""
        self.connect_timeout = 0
        self.remote_sep = "/"
        self._ssh = None

    def _exec_once(
        self, command: str, *, timeout: int, stdin_text: str | None
    ) -> Result:
        proc = subprocess.run(
            # A login shell may print a host banner from /etc/profile; poll() expects
            # stdout to be exactly one JSON document.
            ["bash", "-c", command],
            input=stdin_text,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
        return Result(rc=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)

    def close(self) -> None:
        return None

    def upload_text(self, content: str, remote_path: str) -> None:
        path = os.path.abspath(remote_path)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)

    def upload_file(
        self, local_path: str, remote_path: str, *, mode: int | None = None
    ) -> None:
        source = os.path.abspath(local_path)
        target = os.path.abspath(remote_path)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        if source != target:
            shutil.copy2(source, target)
        if mode is not None:
            os.chmod(target, mode)

    def download_file(self, remote_path: str, local_path: str) -> None:
        self.upload_file(remote_path, local_path)

    def upload_dir(
        self, local_dir: str, remote_dir: str, exclude_dirs: set[str] | None = None
    ) -> int:
        return self._copy_dir(local_dir, remote_dir, exclude_dirs)

    def download_dir(
        self,
        remote_dir: str,
        local_dir: str,
        exclude_patterns: set[str] | tuple[str, ...] | None = None,
    ) -> int:
        return self._copy_dir(remote_dir, local_dir, exclude_patterns)

    @staticmethod
    def _copy_dir(
        source_dir: str, target_dir: str, exclude_dirs: set[str] | None = None
    ) -> int:
        source = os.path.abspath(source_dir)
        target = os.path.abspath(target_dir)
        if not os.path.isdir(source):
            return 0
        count = 0
        for root, dirs, files in os.walk(source):
            if exclude_dirs:
                dirs[:] = [
                    name
                    for name in dirs
                    if not any(fnmatch(name, pattern) for pattern in exclude_dirs)
                ]
            rel = os.path.relpath(root, source)
            dst_root = target if rel == "." else os.path.join(target, rel)
            os.makedirs(dst_root, exist_ok=True)
            for name in files:
                if exclude_dirs and any(
                    fnmatch(name, pattern) for pattern in exclude_dirs
                ):
                    continue
                src = os.path.join(root, name)
                dst = os.path.join(dst_root, name)
                if os.path.abspath(src) != os.path.abspath(dst):
                    shutil.copy2(src, dst)
                count += 1
        return count


def _q(path: str) -> str:
    """POSIX shell quoting for remote paths.

    ``shlex.quote`` is correct for the POSIX guests and is what the previous client used; a Windows
    guest never reaches these calls (submit/poll are POSIX-only by construction -- they run bash).
    """
    import shlex

    return shlex.quote(path)


def get_vm_client(*, local: bool = False, **overrides) -> VmClient:
    """Return the SSH transport unless a test/dev caller explicitly requests local."""
    if local:
        if overrides:
            raise TypeError("LocalClient does not accept SSH overrides")
        return LocalClient()
    return VmClient.from_env(**overrides)


__all__ = [
    "INVOCATION_ROOT",
    "LocalClient",
    "Result",
    "VmClient",
    "get_vm_client",
]
