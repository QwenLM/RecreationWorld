"""Give the agent a network namespace with no way out, and bridge back only what
it legitimately needs.

WHY NOT THE OBVIOUS THINGS
Egress is not a filesystem or capability property, so the existing setpriv
de-privilege cannot touch it: an unprivileged user opens sockets freely, and
dropping capabilities changes nothing. `iptables -m owner --uid-owner` would be
the textbook answer but needs NET_ADMIN and an iptables binary, neither of which
this image has. Per-tool denials (WebFetch/WebSearch off, a browser origin
allowlist, codex's own sandbox) each close one door while `Bash(*)` leaves
curl/wget/python-urllib wide open — and the task prompt itself tells the agent to
use curl against the local site, so banning it breaks the documented workflow.

WHAT THIS DOES INSTEAD
`unshare` creates a network namespace with NO interface except a loopback of its
own. There is no route out, so nothing inside can reach the internet no matter
which binary or language it uses — a kernel property, not a blacklist.

TWO ROUTES, BECAUSE NO SINGLE ONE WORKS EVERYWHERE
`unshare -n` needs CAP_SYS_ADMIN but creates ONLY a netns. `unshare -rn` needs no
privilege but creates a USER namespace too. Measured, the two environments we run
in each allow exactly one:

    dev box (root, CapEff=…a80425fb, no CAP_SYS_ADMIN):  -n EPERM,  -rn ok
    deployment platform job pod (`privileged: false`):                    -n EPERM,  -rn ENOSPC

ENOSPC from unshare is not disk — it is the ucount limit (user.max_user_namespaces
== 0), a common hardening. So in the deployment platform pod BOTH routes were closed and isolation
fell open; the fix is `privileged: true` in the task config, which grants
CAP_SYS_ADMIN and opens the `-n` route (and sidesteps the userns limit entirely).

The two routes differ in WHERE the de-privilege goes, which is why run_prefix is
composed here rather than by the caller: `-r` maps the CURRENT uid to root inside
the new user namespace, so it must already be the agent uid (setpriv FIRST);
`-n` needs CAP_SYS_ADMIN, which the agent uid does not have (setpriv LAST, applied
by the in-namespace entrypoint after it has brought up lo and the relays).

The catch is that the new namespace's 127.0.0.1 is NOT the pod's, so the agent
also loses the two things it must reach: the reference site and the model
endpoint. UNIX domain sockets are filesystem objects and cross namespaces
freely (verified), so each allowed endpoint gets a relay pair:

    outside (pod netns):  UNIX-LISTEN <sock>            -> TCP <real host:port>
    inside  (agent netns): TCP-LISTEN 127.0.0.1:<port>  -> UNIX-CONNECT <sock>

The agent keeps using the same URL it was given. Reachability becomes an explicit
allowlist — everything not in the relay table is simply unroutable.

FAIL-CLOSED BY DEFAULT
If the kernel refuses a namespace, the release run aborts rather than producing
a score with open egress.  Local development may explicitly opt out with
MOCKWEB_NET_ISOLATION=0; evaluation.egress_scan records that state and retains
the egress penalty for such unisolated artifacts.
"""

import asyncio
import fcntl
import json
import logging
import os
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)


def _copy_agent_stdout(source, live_stream, output) -> None:
    """Forward available bytes so completed tools are observed before CLI exit."""
    try:
        while True:
            chunk = source.read1(65536)
            if not chunk:
                break
            live_stream.write(chunk)
            live_stream.flush()
            output.write(chunk)
            output.flush()
    finally:
        live_stream.close()


class _PersistentCaptureClient:
    """Send ordered screenshot requests through one long-lived helper."""

    def __init__(self, command, timeout: float = 10.0):
        self.command = list(command)
        self.timeout = timeout
        self.process = None

    def _start(self) -> None:
        self.process = subprocess.Popen(
            self.command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )

    def capture(self, output: Path) -> tuple[bool, str]:
        if self.process is None or self.process.poll() is not None:
            self.close()
            self._start()
        assert self.process is not None
        assert self.process.stdin is not None
        assert self.process.stdout is not None
        try:
            request = json.dumps({"output": str(output)}).encode() + b"\n"
            self.process.stdin.write(request)
            self.process.stdin.flush()
            ready, _, _ = select.select(
                [self.process.stdout.fileno()], [], [], self.timeout
            )
            if not ready:
                self.close()
                return False, (
                    f"persistent screenshot helper timed out after {self.timeout}s"
                )
            line = self.process.stdout.readline()
            if not line:
                returncode = self.process.poll()
                self.close()
                return False, f"persistent screenshot helper exited ({returncode})"
            response = json.loads(line)
            if not isinstance(response, dict):
                raise ValueError("persistent screenshot helper returned non-object JSON")
            ok, detail = bool(response.get("ok")), str(response.get("error") or "")
            if not ok:
                self.close()
            return ok, detail
        except (BrokenPipeError, OSError, ValueError) as exc:
            self.close()
            return False, (
                "persistent screenshot helper failed: "
                f"{type(exc).__name__}: {exc}"
            )

    def close(self) -> None:
        process, self.process = self.process, None
        if process is None:
            return
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


_SIOCGIFFLAGS, _SIOCSIFFLAGS, _IFF_UP = 0x8913, 0x8914, 0x1

ISOLATION_ENV = "MOCKWEB_NET_ISOLATION"   # "0"/"off" disables
RELAY_DIR_ENV = "MOCKWEB_RELAY_DIR"

# Isolation routes, in the order preflight() prefers them. NETNS first: it needs
# no user namespace, so it is immune to the ucount limit that closed USERNS in
# the deployment platform pod, and it leaves the agent's uid mapping untouched.
NETNS = "netns"     # `unshare -n`  — needs CAP_SYS_ADMIN; de-privilege INSIDE
USERNS = "userns"   # `unshare -rn` — needs no privilege; de-privilege OUTSIDE


class IsolationUnavailable(RuntimeError):  # noqa: N818 - public compatibility
    """Isolation was required but could not be established.

    Raised rather than warned because the alternative — a rollout that quietly
    runs with open egress — produces a SCORE, and a score from an unisolated run
    is indistinguishable downstream from a legitimate one. A regression once logged
    "network isolation OFF ... No space left on device" and scored normally. Failing the job
    makes the infra problem impossible to miss and impossible to average into a result.
    """


def isolation_requested() -> bool:
    """Whether isolation is wanted here — and therefore also REQUIRED.

    One switch for both, deliberately: "try to isolate but carry on if you
    cannot" is the mode that let an unisolated run score. Set
    MOCKWEB_NET_ISOLATION=0 for local development where no namespace is
    available; anything else means the rollout must be isolated or not happen.
    """
    return os.environ.get(ISOLATION_ENV, "1").strip().lower() not in ("0", "false", "no", "off")


_SEAL_PROBE = Path(__file__).resolve().parents[1] / "runtime_assets/netns_seal_probe.py"


def _verify_sealed(route: str) -> tuple[bool, str]:
    """Prove the namespace cannot reach the internet, instead of assuming it.

    `unshare` exiting 0 only says a namespace was created. It does not say the
    namespace has no route — a future kernel/runtime could hand us one with
    connectivity, and then everything downstream would believe in an isolation
    that is not there. So the route is only accepted once a connect to a public
    address has actually been refused inside it. In a routeless namespace that
    fails immediately with ENETUNREACH, so this costs milliseconds, not the
    timeout.
    """
    flag = "-n" if route == NETNS else "-rn"
    try:
        r = subprocess.run(["unshare", flag, "--", sys.executable, str(_SEAL_PROBE)],
                           capture_output=True, timeout=40, text=True)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"seal check could not run: {type(e).__name__}"
    out = (r.stdout or "").strip()
    if r.returncode != 0:
        return False, f"seal check exited {r.returncode}: {(r.stderr or '').strip()[:120]}"
    if out.startswith("LEAK"):
        return False, f"namespace STILL REACHES the internet ({out})"
    if out != "SEALED":
        return False, f"seal check output unrecognised: {out[:60]!r}"
    return True, "sealed"


def bring_up_loopback() -> None:
    """Bring `lo` up inside the current netns.

    Done with an ioctl rather than `ip link set lo up` so the image does not need
    iproute2. A fresh namespace's loopback is DOWN (measured — binding 127.0.0.1
    there fails with ENETUNREACH), and `unshare -r` makes us root INSIDE the user
    namespace, which is what grants CAP_NET_ADMIN over this namespace only.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        ifr = struct.pack("16sh", b"lo", 0)
        flags = struct.unpack("16sh", fcntl.ioctl(s, _SIOCGIFFLAGS, ifr))[1]
        fcntl.ioctl(s, _SIOCSIFFLAGS, struct.pack("16sh", b"lo", flags | _IFF_UP))


def _probe(args) -> tuple[bool, str]:
    try:
        r = subprocess.run(args, capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"probe failed: {type(e).__name__}"
    if r.returncode != 0:
        return False, (r.stderr or b"").decode("utf-8", "replace").strip()[:100] or "nonzero exit"
    return True, "ok"


def _ns_limits() -> str:
    """The three numbers that explain any unshare refusal.

    Worth collecting eagerly on failure: eval can run in a worker with no console, so if the log
    does not carry this, diagnosing why
    isolation fell open costs a whole extra job.
    """
    bits = []
    for name in ("max_user_namespaces", "max_net_namespaces"):
        try:
            bits.append(f"{name}={Path('/proc/sys/user', name).read_text().strip()}")
        except OSError:
            bits.append(f"{name}=?")
    cap = "?"
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("CapEff:"):
                cap = line.split()[1]
                break
    except OSError:
        pass
    bits.append(f"uid={os.getuid()} CapEff={cap}")
    return " ".join(bits)


def preflight() -> tuple[str, str]:
    """Which isolation route is available here? (route|"", reason).

    Tries NETNS before USERNS — see the module docstring: a plain netns needs no
    user namespace, so it is unaffected by user.max_user_namespaces, which is the
    limit that closed the USERNS route in the deployment platform pod.
    """
    if not isolation_requested():
        return "", f"disabled via {ISOLATION_ENV}"
    if not shutil.which("unshare"):
        return "", "unshare(1) not present"
    routes, why_n, why_r = [], "", ""
    ok, why_n = _probe(["unshare", "-n", "true"])
    if ok:
        routes.append((NETNS, "netns route (CAP_SYS_ADMIN)"))
    ok, why_r = _probe(["unshare", "-rn", "true"])
    if ok:
        routes.append((USERNS, "userns route (unprivileged)"))
    if not routes:
        return "", (f"no namespace route: `unshare -n` -> {why_n}; "
                    f"`unshare -rn` -> {why_r}; {_ns_limits()}")

    # A route is only usable once it is PROVEN sealed, so a namespace that somehow
    # carries connectivity is rejected rather than trusted.
    reasons = []
    for route, label in routes:
        sealed, seal_why = _verify_sealed(route)
        if sealed:
            return route, label
        reasons.append(f"{route}: {seal_why}")
        logger.warning("isolation route %s rejected — %s", route, seal_why)
    return "", "no route passed the seal check: " + "; ".join(reasons)


# ── relay ──────────────────────────────────────────────────────────────────────

async def _pump(reader, writer):
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionResetError, BrokenPipeError, asyncio.IncompleteReadError):
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _bridge(a_r, a_w, b_r, b_w):
    # Both directions run concurrently and independently: the model endpoint is a
    # long-lived streaming (SSE) connection, so a half-close must not tear down
    # the other direction.
    await asyncio.gather(_pump(a_r, a_w), _pump(b_r, b_w), return_exceptions=True)


async def _serve_unix_to_tcp(sock_path: str, host: str, port: int):
    async def handle(r, w):
        try:
            tr, tw = await asyncio.open_connection(host, port)
        except OSError as e:
            logger.warning("relay %s -> %s:%s connect failed: %s", sock_path, host, port, e)
            w.close()
            return
        await _bridge(r, tw, tr, w)
    Path(sock_path).parent.mkdir(parents=True, exist_ok=True)
    if os.path.exists(sock_path):
        os.unlink(sock_path)
    server = await asyncio.start_unix_server(handle, path=sock_path)
    # The agent runs as a different uid and must be able to connect.
    os.chmod(sock_path, 0o777)
    return server


async def _serve_tcp_to_unix(port: int, sock_path: str):
    async def handle(r, w):
        try:
            ur, uw = await asyncio.open_unix_connection(path=sock_path)
        except OSError as e:
            logger.warning("relay 127.0.0.1:%s -> %s connect failed: %s", port, sock_path, e)
            w.close()
            return
        await _bridge(r, uw, ur, w)
    return await asyncio.start_server(handle, host="127.0.0.1", port=port)


def parse_relay_spec(spec: str) -> tuple[int, str]:
    """"<port>=<unix socket path>" -> (port, path)."""
    port, _, path = spec.partition("=")
    return int(port), path


# ── outside half (orchestrator side) ───────────────────────────────────────────

class OutsideRelays:
    """Runs UNIX->TCP relays in a background thread for the agent's lifetime.

    A thread with its own event loop, because the orchestrator's loop is busy
    awaiting the agent subprocess and these must keep serving throughout.
    """

    def __init__(self, table):
        # table: [(port, sock_path, real_host, real_port)]
        self.table = list(table)
        self._loop = None
        self._thread = None
        self._servers = []
        self._start_error = None
        self._stop_event = None

    def start(self):
        import threading
        ready = threading.Event()
        self._stop_event = threading.Event()

        def run():
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)

            async def serve():
                for _port, sock, host, rport in self.table:
                    self._servers.append(
                        await _serve_unix_to_tcp(sock, host, rport)
                    )
                    logger.info("relay up: %s -> %s:%s", sock, host, rport)
                ready.set()
                while not self._stop_event.is_set():
                    await asyncio.sleep(0.1)
            try:
                self._loop.run_until_complete(serve())
            except BaseException as exc:  # surfaced synchronously by start()
                self._start_error = exc
                ready.set()
            finally:
                for server in self._servers:
                    server.close()
                self._loop.close()

        self._thread = threading.Thread(target=run, name="agent-net-relays", daemon=True)
        self._thread.start()
        if not ready.wait(timeout=30):
            self.stop()
            raise RuntimeError("relays did not come up within 30s")
        if self._start_error is not None:
            self.stop()
            raise RuntimeError(f"relay startup failed: {self._start_error}") from self._start_error

    def stop(self):
        if self._stop_event is not None:
            self._stop_event.set()
        if self._loop is not None and not self._loop.is_closed():
            # Wake the selector so serve() observes _stop_event immediately.
            try:
                self._loop.call_soon_threadsafe(lambda: None)
            except RuntimeError:
                pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=5)
        for _port, sock, _h, _p in self.table:
            try:
                os.unlink(sock)
            except OSError:
                pass


def wrap_cmd(cmd: list, relay_specs: list, route: str = "", run_prefix=()) -> list:
    """Compose the full argv: de-privilege + namespace + relays + `cmd`.

    `run_prefix` is the setpriv wrapper from run_agent._agent_run_prefix. It is
    passed in rather than applied by the caller because its POSITION depends on
    the route and getting it wrong fails in opposite ways:

      NETNS  — `unshare -n` needs CAP_SYS_ADMIN, which the agent uid lacks, so we
               unshare as root and the in-namespace entrypoint (which also needs
               root for the lo ioctl) drops privilege around `cmd` itself.
      USERNS — `-r` maps the CURRENT uid to root inside the new user namespace, so
               it must already BE the agent uid; setpriv goes outermost.

    An empty route means isolation is off: `cmd` still gets de-privileged.
    """
    capture_enabled = bool(os.environ.get("MOCKWEB_CAPTURE_BROWSER_CMD_JSON", ""))
    if not route and not capture_enabled:
        return list(run_prefix) + list(cmd)
    inner = [sys.executable, os.path.abspath(__file__)]
    if not route:
        inner.append("--skip-loopback")
    for spec in relay_specs:
        inner += ["--relay", spec]
    if route == NETNS:
        return ["unshare", "-n", "--"] + inner + ["--"] + list(run_prefix) + list(cmd)
    if route == USERNS:
        return list(run_prefix) + ["unshare", "-rn", "--"] + inner + ["--"] + list(cmd)
    if not route:
        return inner + ["--"] + list(run_prefix) + list(cmd)
    raise ValueError(f"unknown isolation route {route!r}")


# ── inside half (entrypoint, runs inside the namespace) ────────────────────────

def _main(argv) -> int:
    relays, rest = [], []
    skip_loopback = False
    i = 0
    while i < len(argv):
        if argv[i] == "--relay":
            relays.append(argv[i + 1])
            i += 2
        elif argv[i] == "--skip-loopback":
            skip_loopback = True
            i += 1
        elif argv[i] == "--":
            rest = argv[i + 1:]
            break
        else:
            i += 1
    if not rest:
        print("net_isolate: no command given", file=sys.stderr)
        return 2

    try:
        if not skip_loopback:
            bring_up_loopback()
    except OSError as e:
        # Without loopback the agent cannot reach even the relays, so this is
        # fatal for the isolated path — surface it instead of running blind.
        print(f"net_isolate: cannot bring up lo: {e}", file=sys.stderr)
        return 3

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)

    async def boot():
        for spec in relays:
            port, sock = parse_relay_spec(spec)
            await _serve_tcp_to_unix(port, sock)
    loop.run_until_complete(boot())

    # Relays must outlive this call, so the agent is a CHILD, not an exec target.
    # Forward direct termination too (for example a pod shutdown).  The outer
    # runner also puts this wrapper and its child in one process group so its
    # timeout path can terminate the complete tree rather than orphaning the CLI.
    companion = None
    capture_monitor = None
    capture_helper = None
    stdout_thread = None
    try:
        companion_argv = json.loads(
            os.environ.get("MOCKWEB_CAPTURE_BROWSER_CMD_JSON", "[]")
        )
        if companion_argv:
            companion = subprocess.Popen(
                companion_argv,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                try:
                    with socket.create_connection(("127.0.0.1", 9222), timeout=0.2):
                        break
                except OSError:
                    if companion.poll() is not None:
                        raise RuntimeError("capture browser exited before CDP was ready")
                    time.sleep(0.1)
            else:
                raise RuntimeError("capture browser CDP was not ready within 20s")

            sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
            from core.tool_use_capture import ToolUseCaptureMonitor

            capture_helper = _PersistentCaptureClient(
                json.loads(os.environ["MOCKWEB_CAPTURE_COMMAND_JSON"]), timeout=20
            )
            capture_monitor = ToolUseCaptureMonitor(
                os.environ["MOCKWEB_CAPTURE_TRAJECTORY"],
                os.environ["MOCKWEB_CAPTURE_OUTPUT_DIR"],
                capture=capture_helper.capture,
                timeout_sec=20,
            ).start()
        if companion_argv:
            live_trajectory = Path(os.environ["MOCKWEB_CAPTURE_TRAJECTORY"])
            live_trajectory.parent.mkdir(parents=True, exist_ok=True)
            live_stream = live_trajectory.open("wb")
            proc = subprocess.Popen(rest, stdout=subprocess.PIPE)

            def copy_stdout() -> None:
                assert proc.stdout is not None
                _copy_agent_stdout(proc.stdout, live_stream, sys.stdout.buffer)

            stdout_thread = threading.Thread(
                target=copy_stdout, name="agent-stdout-capture", daemon=True
            )
            stdout_thread.start()
        else:
            proc = subprocess.Popen(rest)
    except Exception as exc:
        print(f"net_isolate: capture setup failed open: {exc}", file=sys.stderr)
        proc = subprocess.Popen(rest)

    def forward(signum, _frame):
        if proc.poll() is None:
            try:
                proc.send_signal(signum)
            except ProcessLookupError:
                pass

    previous_handlers = {}
    for signum in (signal.SIGTERM, signal.SIGINT):
        previous_handlers[signum] = signal.signal(signum, forward)

    async def wait():
        while proc.poll() is None:
            await asyncio.sleep(0.2)
        return proc.returncode
    try:
        return loop.run_until_complete(wait())
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        if stdout_thread is not None:
            stdout_thread.join(timeout=10)
        if capture_monitor is not None:
            capture_monitor.stop()
        if capture_helper is not None:
            capture_helper.close()
        if companion is not None and companion.poll() is None:
            companion.terminate()
            try:
                companion.wait(timeout=5)
            except subprocess.TimeoutExpired:
                companion.kill()
                companion.wait()
        loop.close()


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
