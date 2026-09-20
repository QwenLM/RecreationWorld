"""One cua-driver policy for the three platforms that drive a GUI.

linux, windows and macOS each had their own copy of "which driver, which version, which
coordinate convention, where to fetch it" — and they had drifted to three different drivers:

    linux    trycua cua-driver-rs 0.7.1   built from source   coord space 0 (pixel)
    windows  trycua cua-driver    0.5.1   IMAGE-BAKED         coord space 0 (pixel)
    macos    qwen-cua-driver      0.7.3   app bundle          coord space 1 (normalized)

That is not a cosmetic spread: a 12-point score difference has been measured between driver
versions, so an unpinned or divergent driver is an untracked confound in every cross-platform
comparison. Android drives its emulator through mobile-mcp; Web defaults to Playwright but
also exposes an intentional cua-driver mode, which follows this same pin.

The unified pin is the QWEN fork, because it is the only one that can serve all three: the
0-1000 window-normalized coordinate mode exists only in that fork, so macOS — whose setup
depends on it — cannot move to trycua. The binary itself is coord-agnostic (normalized vs pixel
is chosen by env, not by build), so unifying the VERSION does not force a coordinate change;
each platform's coordinate space stays its own decision.

Shared here: ref parsing, the env contract, and the prebuilt cache key. NOT shared: the install
transport, which is genuinely per-platform — cargo/prebuilt over ssh on linux, install.ps1 over
paramiko on windows, an .app bundle over scp on macOS.
"""

from __future__ import annotations

# The unified pin. Both halves matter: the fork AND the version.
DEFAULT_SOURCE = "qwen"
DEFAULT_VERSION = "0.7.3"

# `install.sh`/`install.ps1` live in these repos; the platform picks the right extension.
# ``{ref}`` is the git ref the SCRIPT is read from, which is a separate pin from the archive the
# script installs.  Only the qwen fork needs the two tied together: on 2026-09-01 its installer on
# main grew a hard `throw "release archive is missing required qwen-cua-driver-uia.exe"` that no
# 0.7.3 archive satisfies, so every windows job pinned to qwen:0.7.3 failed driver verification
# while rb and deployment adapter were unchanged.  Reading that script from the release tag makes the two
# move together.  trycua's script on main installed 0.7.1 cleanly in the same run and stays
# unpinned, so a new trycua release does not need a tag to exist before it can be used.
INSTALL_REPOS = {
    "trycua": "https://raw.githubusercontent.com/trycua/cua/{ref}/libs/cua-driver/scripts/install",
    "qwen": "https://raw.githubusercontent.com/QwenLM/qwen-code/{ref}/packages/cua-driver/scripts/install",
}
RELEASE_TAG = "cua-driver-rs-v{version}"
INSTALL_REFS = {"trycua": "main", "qwen": RELEASE_TAG}
BIN_NAMES = {"trycua": "cua-driver", "qwen": "qwen-cua-driver"}

# Public, immutable-by-content macOS application bundles.  Keep the digest beside the URL so
# switching a release can never silently change the executable that receives TCC permissions.
# The full archive (rather than the ``-binary`` asset) contains QwenCuaDriver.app.
MACOS_RELEASES = {
    "0.7.3": {
        "url": (
            "https://github.com/QwenLM/qwen-code/releases/download/"
            "cua-driver-rs-v0.7.3/cua-driver-rs-0.7.3-darwin-universal.tar.gz"
        ),
        "sha256": "814a7d83d5f23daac7cda3b3553c5a2f7a956f82f88d843c7829b7a8691e3534",
        "source_commit": "cb483092561a3606dfd53447a26f3718380f0f59",
        "license": "Apache-2.0",
    }
}

# Env the DAEMON must be started with. Seeding only the MCP front-end is a no-op: the `mcp`
# command proxies to the running daemon, and the coordinate transforms take effect there.
ENV_PREFIX = "CUA_DRIVER_RS_"
DEFAULT_SCALE = "1000"
# Keep the trusted daemon alive for the full 24h pod/sandbox lifetime. This is not an
# agent budget: it only prevents a long compile or model turn from aging out the UI bridge.
DEFAULT_IDLE_TTL = "86400"

# qwen-cua-driver 0.7.3 also exposes a ``cua-driver-uia`` helper pipe on Windows and its
# automatic MCP routing prefers that pipe whenever WaitNamedPipe says it exists. A stale helper
# pipe can satisfy that shallow check while never answering the proxy's initial daemon ``list``
# request. Pin benchmark clients to the long-lived daemon pipe so startup health is deterministic.
WINDOWS_DAEMON_PIPE = r"\\.\pipe\cua-driver"


def parse_ref(ref: str) -> tuple[str, str, bool]:
    """``ref`` -> ``(source, version, normalize)``.

    Accepts every spelling the platforms already use::

        cua-driver-rs-v0.7.1   -> ("trycua", "0.7.1", False)
        qwen:0.7.3             -> ("qwen",   "0.7.3", False)
        qwen-0.7.3             -> ("qwen",   "0.7.3", False)
        qwen:0.7.3:norm        -> ("qwen",   "0.7.3", True)
        0.7.3                  -> (DEFAULT_SOURCE, "0.7.3", False)

    The ``:norm`` / ``+norm`` suffix exists because linux encodes the coordinate mode in the
    ref rather than adding a second deployment platform param; it is parsed here so windows and macOS can honour
    the same spelling instead of inventing their own.
    """
    ref = (ref or "").strip()
    if not ref:
        return "", "", False
    normalize = False
    for suffix in (":norm", "+norm"):
        if ref.endswith(suffix):
            normalize = True
            ref = ref[: -len(suffix)]
    low = ref.lower()
    if low.startswith("qwen:") or low.startswith("qwen-"):
        return "qwen", ref[5:], normalize
    if low.startswith("cua-driver-rs-v"):
        return "trycua", ref[len("cua-driver-rs-v") :], normalize
    if low.startswith("trycua:"):
        return "trycua", ref[7:], normalize
    return DEFAULT_SOURCE, ref, normalize


def driver_env(
    *,
    normalize: bool = False,
    scale: str = DEFAULT_SCALE,
    idle_ttl: str = DEFAULT_IDLE_TTL,
    update_check: str = "0",
) -> dict[str, str]:
    """The CUA_DRIVER_RS_* env every platform must give the daemon.

    ``normalize`` picks the coordinate SPACE: 1 = 0-1000 window-normalized (macOS today),
    0 = real pixels (linux/windows today). update_check is off because a driver that upgrades
    itself mid-benchmark is the confound this module exists to prevent.
    """
    return {
        f"{ENV_PREFIX}COORDINATE_SPACE": "1" if normalize else "0",
        f"{ENV_PREFIX}COORDINATE_SCALE": str(scale),
        f"{ENV_PREFIX}SESSION_IDLE_TTL_SECS": str(idle_ttl),
        f"{ENV_PREFIX}UPDATE_CHECK": str(update_check),
    }


# --- the daemon / bridge split ---------------------------------------------------------------
# 0.7.3 is already two processes: `serve` holds the native A11y/UIA/AX + input rights, and `mcp`
# proxies to it when a daemon is listening. That is what makes a Gateway insertable rather than a
# rewrite. But the proxy is only a boundary if the fallback is closed, and today it is not:
#
#   macOS    `serve --socket` + `mcp --socket` + MCP_FORCE_PROXY=1   -> fail-closed already
#   windows  `autostart kick` starts serve, but the agent's MCP entry carries neither a socket
#            nor FORCE_PROXY                                        -> silent in-process fallback
#   linux    recreation starts NO daemon and spawns `mcp --no-overlay` in the agent's own
#            privilege domain                                       -> in-process is the ONLY path
#
# Hence the ORDER, which is easy to get backwards: a trusted daemon must exist BEFORE force-proxy
# is switched on. Enabling FORCE_PROXY on linux today would fail-close every single run.


def daemon_socket(run_id: str, *, root: str = "/tmp") -> str:
    """Per-run daemon endpoint. Per-run, not shared, so Seal can destroy it.

    A shared socket cannot express a run-scoped capability: whoever can reach it can drive the
    desktop after the run it belongs to has been sealed.
    """
    safe = "".join(c if (c.isalnum() or c in "-_") else "-" for c in str(run_id))[:64]
    return f"{root.rstrip('/')}/rb-cua-{safe}.sock"


def bridge_env(socket_path: str, **kw) -> dict[str, str]:
    """Env for the MCP bridge so it PROXIES and never executes in-process.

    FORCE_PROXY is the load-bearing entry: without it an unreachable daemon silently promotes the
    bridge into a driver holding native rights inside the agent's own privilege domain, so the
    failure mode of the isolation is a privilege escalation rather than an error.
    """
    env = driver_env(**kw)
    env[f"{ENV_PREFIX}MCP_FORCE_PROXY"] = "1"
    env[f"{ENV_PREFIX}SOCKET"] = socket_path
    return env


# The socket must be reachable by the agent's uid and nobody else. 0o777 would let any account on
# the box drive the desktop, which is the same hole as an in-process fallback by a different route.
SOCKET_MODE = 0o600


def serve_argv(binary: str, socket_path: str) -> list[str]:
    """Argv for the trusted daemon. Started by the Runner, as a user the agent is NOT."""
    return [binary, "serve", "--socket", socket_path]


def install_url(source: str, ext: str, version: str) -> str:
    """``ext`` is "sh" (linux/macOS) or "ps1" (windows); ``version`` pins the qwen script.

    ``version`` is required rather than optional even though only one fork consumes it: making
    it a parameter every caller must supply is what stops a future caller from silently getting
    an installer that can outrun the archive it installs.
    """
    repo = INSTALL_REPOS.get(source)
    if not repo:
        raise ValueError(
            f"unknown cua-driver source {source!r}; expected {sorted(INSTALL_REPOS)}"
        )
    ref = INSTALL_REFS[source]
    if "{version}" in ref:
        version = (version or "").strip()
        if not version:
            raise ValueError(
                "cua-driver install_url requires the pinned release version"
            )
        ref = ref.format(version=version)
    return f"{repo.format(ref=ref)}.{ext}"


def macos_release(version: str) -> tuple[str, str]:
    """Return the public macOS app archive URL and its release SHA-256."""
    version = (version or "").strip()
    try:
        release = MACOS_RELEASES[version]
    except KeyError as exc:
        raise ValueError(
            f"no pinned macOS cua-driver archive for version {version!r}"
        ) from exc
    return release["url"], release["sha256"]


def prebuilt_artifact_key(
    source: str,
    version: str,
    arch: str = "x86_64",
    prefix: str = "RecreationBench/bin",
) -> str:
    """Self-priming prebuilt cache. At high concurrency every job fetching the driver from
    raw.githubusercontent.com rate-limits GitHub and bootstrap fails, so the first miss uploads
    here. Keyed on version only, because the binary is coord-agnostic."""
    stem = BIN_NAMES.get(source, source)
    tag = version if source == "qwen" else f"cua-driver-rs-v{version}"
    return f"{prefix}/{stem}-{tag}-{arch}"
