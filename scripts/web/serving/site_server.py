"""Static site HTTP server with CORS support.

Serves a site directory over HTTP for agent browsing and evaluation.
Based on v1 extraction/server.py with added CORS and caching headers.
"""

import asyncio
import logging
import os
import random
import signal
import socket
import subprocess
import threading
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)

_port_lock = threading.Lock()
_allocated_ports: set[int] = set()


def find_free_port(start: int = 8100, end: int = 9100, exclude: set[int] | None = None) -> int:
    """Find a free port in the given range with thread-safe allocation.

    Scans the range in a RANDOMISED order (seeded per process) rather than always
    starting at `start`. When many gt-build processes launch a SiteServer at the same
    moment (concurrent workers sharing a network namespace), a deterministic 8100-first
    scan makes them all pick the same low ports and collide on the bind-test race window.
    Randomising the probe order spreads them across the range, drastically cutting the
    collision rate; the post-start identity check in SiteServer.start() is the hard
    guarantee that a collision can never silently serve the wrong station.
    """
    exclude = exclude or set()
    ports = list(range(start, end))
    random.shuffle(ports)
    with _port_lock:
        for port in ports:
            if port in _allocated_ports or port in exclude:
                continue
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                try:
                    s.bind(("", port))
                    _allocated_ports.add(port)
                    return port
                except OSError:
                    continue
        raise RuntimeError(f"No free port found in range {start}-{end}")


def _release_port(port: int):
    with _port_lock:
        _allocated_ports.discard(port)


# Run the child from a source file so the HTTP behavior is reviewable and testable
# independently from the process supervisor.
_HANDLER_PATH = Path(__file__).with_name("static_handler.py")


class SiteServer:
    """Serve a static site directory over HTTP with CORS support.

    Usage:
        async with SiteServer("/path/to/site") as server:
            print(server.url)  # http://localhost:XXXX
    """

    def __init__(self, site_dir: str, port: int = None):
        self.site_dir = str(Path(site_dir).resolve())
        self._fixed_port = port
        # Ephemeral (port 0) by DEFAULT. The serving socket itself asks the OS for a
        # guaranteed-free port and HOLDS it for the whole server lifetime. This removes the
        # find_free_port() "bind-test, close, then re-bind in a subprocess" TOCTOU window that
        # let two concurrent GT-build pods (shared network namespace) land on the same port —
        # the 2026-07-20 cross-station GT contamination. With no SO_REUSEPORT, no other pod can
        # bind this port while this server is alive, so the port cannot be stolen mid-capture.
        # An explicit `port=` (dev tools like serve_dataset.py) still binds that exact port.
        self._requested_port = 0 if port is None else port
        self.port = port  # authoritative value is set from the child's BOUND_PORT after launch
        self.process = None
        self.url = f"http://localhost:{port}" if port else None

    def _launch_handler(self) -> bool:
        """Popen the CORS handler subprocess, learn the port it actually bound, and wait
        until it answers.

        The subprocess prints `BOUND_PORT <n>` on stdout immediately after a successful bind.
        For the default ephemeral mode (self._requested_port == 0) that is the OS-assigned port
        this server now owns; we adopt it as self.port/self.url so Playwright talks to exactly the
        port this live socket holds (no separate pick-then-bind step that could race). A failed
        bind exits the child without printing, which we report as a launch failure.
        """
        self.process = subprocess.Popen(
            ["python3", str(_HANDLER_PATH), str(self._requested_port), self.site_dir],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            text=True,
        )
        # Wait (bounded) for the BOUND_PORT line. bind is synchronous and near-instant, so this
        # resolves in milliseconds on success; on a bind failure the child exits and the pipe
        # reaches EOF, so readline() returns '' promptly. select() guards against a wedged child.
        import select
        try:
            ready, _, _ = select.select([self.process.stdout], [], [], 15)
        except (OSError, ValueError):
            ready = []
        if not ready:
            return False
        line = self.process.stdout.readline()
        if not line.startswith("BOUND_PORT"):
            return False
        try:
            self.port = int(line.split()[1])
        except (IndexError, ValueError):
            return False
        self.url = f"http://localhost:{self.port}"
        for _ in range(30):
            try:
                with socket.create_connection(("localhost", self.port), timeout=1):
                    return True
            except (ConnectionRefusedError, OSError):
                pass
            # small async-free sleep — start() may run before a loop exists in some callers
            import time as _t
            _t.sleep(0.2)
        return False

    def _identity_status(self) -> str:
        """Classify what is CURRENTLY answering on self.port for THIS station.

        Returns:
          "ok"      — the server on self.port serves THIS station's tree (its /index.html byte-
                      matches the on-disk one), OR (no root index.html to fingerprint) our own
                      handler subprocess is alive.
          "foreign" — a DIFFERENT station's server is answering (index.html mismatch), or nothing
                      is answering / our subprocess died and something else may have taken the port.
          "unknown" — a transient fetch error (e.g. the handler was momentarily busy). Callers
                      treat this as non-fatal (do NOT abort a build over a hiccup).

        THE port-collision guard. Every cleanroom station's root index.html is unique, so a foreign
        server (whether it won the port at start, or the OS re-handed our freed port to another pod
        after our server died mid-run) fails the byte-match and is caught here.
        """
        idx = Path(self.site_dir) / "index.html"
        try:
            disk = idx.read_bytes() if idx.is_file() else None
        except OSError:
            disk = None
        if disk is None:
            # no fingerprint available: at least require OUR handler subprocess to be alive
            # (a lost-race / crashed handler is dead, so something else may hold the port).
            alive = self.process is not None and self.process.poll() is None
            return "ok" if alive else "foreign"
        # If our own handler subprocess has died, the port is no longer ours — treat as foreign
        # even before fetching (something else may have rebound the freed port).
        if self.process is None or self.process.poll() is not None:
            return "foreign"
        served = None
        for _attempt in range(2):
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{self.port}/index.html")
                with urllib.request.urlopen(req, timeout=8) as r:
                    served = r.read()
                break
            except Exception as e:
                last_err = e
                import time as _t
                _t.sleep(0.3)
        if served is None:
            logger.warning("identity fetch failed on port %d: %s", self.port, str(last_err)[:100])
            return "unknown"
        if served == disk:
            return "ok"
        logger.warning(
            "PORT COLLISION: port %d is serving a FOREIGN site (index.html mismatch, "
            "%d served vs %d on disk for %s)",
            self.port, len(served), len(disk), self.site_dir)
        return "foreign"

    def _serves_this_site(self) -> bool:
        """True iff the server on self.port is confirmed to serve THIS station (start-time guard).

        Treats a transient fetch error ("unknown") as not-yet-confirmed so start() retries on a
        fresh port rather than trusting an unverified server.
        """
        return self._identity_status() == "ok"

    async def assert_serving(self, when: str = "") -> None:
        """Re-assert that self.port STILL serves this station; raise loudly on foreign content.

        Defense-in-depth for per-page captures: the ephemeral-port bind makes a mid-run port
        steal essentially impossible (our live socket holds the port), but if our server ever died
        and the OS re-handed the freed port to another pod, this fails the capture LOUD (raises)
        instead of silently saving foreign pixels. A transient fetch hiccup ("unknown") is logged
        but does NOT abort — only a definite index.html mismatch (or a dead server) raises.
        """
        status = await asyncio.get_event_loop().run_in_executor(None, self._identity_status)
        if status == "foreign":
            raise RuntimeError(
                f"SiteServer identity re-check FAILED{(' ' + when) if when else ''}: port "
                f"{self.port} no longer serves {self.site_dir} (foreign/dead server) — aborting "
                f"capture to avoid cross-station GT contamination")

    async def start(self) -> str:
        """Start the server, VERIFY it serves this station's tree, and return the base URL.

        Default (ephemeral) mode: the child binds port 0, the OS hands it a guaranteed-free port
        with no race window, and we adopt whatever it reports. The start-time identity check is
        kept as belt-and-suspenders; on the astronomically rare miss we simply relaunch (the next
        port-0 bind gives a different free port). An explicit `port=` does a single attempt and
        raises loudly on collision instead of silently serving the wrong station.
        """
        attempts = 1 if self._fixed_port is not None else 8
        for _ in range(attempts):
            up = await asyncio.get_event_loop().run_in_executor(None, self._launch_handler)
            if up and self._serves_this_site():
                logger.info("Server started at %s serving %s", self.url, self.site_dir)
                return self.url
            # collision or failed launch: tear down this attempt and relaunch. In ephemeral mode
            # the next port-0 bind picks a different free port; a fixed port cannot be retried.
            await self.stop()
            if self._fixed_port is not None:
                break

        raise RuntimeError(
            f"Server failed to start a self-consistent instance for {self.site_dir} "
            f"(last port {self.port}); likely persistent port contention")

    async def stop(self):
        """Stop the server and release the port."""
        if self.process:
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
            try:
                await asyncio.get_event_loop().run_in_executor(
                    None, lambda: self.process.wait(timeout=5)
                )
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
                    await asyncio.get_event_loop().run_in_executor(
                        None, lambda: self.process.wait(timeout=2)
                    )
                except (ProcessLookupError, OSError, subprocess.TimeoutExpired):
                    pass
            except Exception:
                pass
            try:
                if self.process.stdout:
                    # Close the BOUND_PORT pipe to avoid leaking an fd on retry.
                    self.process.stdout.close()
            except Exception:
                pass
            self.process = None
            _release_port(self.port)
            logger.info("Server stopped on port %s", self.port)

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, *args):
        await self.stop()
