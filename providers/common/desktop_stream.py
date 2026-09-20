"""E2B Desktop stream helpers used by visual RecreationBench runs."""

from __future__ import annotations

import argparse
import re
from typing import Any


def parse_geometry(value: str | None) -> tuple[int, int]:
    if not value:
        return 1280, 720
    match = re.match(r"^(\d+)x(\d+)(?:x\d+)?$", value.strip())
    if not match:
        return 1280, 720
    return int(match.group(1)), int(match.group(2))


def create_desktop_sandbox(options: dict[str, Any], *, display: str = ":99", geometry: str | None = None):
    """Create a sandbox through e2b-desktop so .stream matches the FC demo."""

    try:
        from e2b_desktop import Sandbox as DesktopSandbox
    except Exception as exc:  # pragma: no cover - depends on optional package
        raise RuntimeError("e2b-desktop is required for visual desktop stream") from exc

    opts = dict(options)
    envs = dict(opts.get("envs") or {})
    envs["DISPLAY"] = display
    envs["RB_VISUAL_MODE"] = "desktop_stream"
    opts["envs"] = envs
    opts.setdefault("secure", True)
    width, height = parse_geometry(geometry)
    return DesktopSandbox.create(
        resolution=(width, height),
        display=display,
        **opts,
    )


def install_novnc_compat(sandbox: Any) -> None:
    """Install the /opt/noVNC layout expected by e2b-desktop's stream helper."""

    command = r"""
set -e
if ! command -v x11vnc >/dev/null 2>&1 || ! command -v websockify >/dev/null 2>&1 || [ ! -d /usr/share/novnc ]; then
  sudo -n mkdir -p /var/lib/apt/lists/partial
  sudo -n apt-get update
  sudo -n apt-get install -y --no-install-recommends x11vnc novnc websockify netcat-openbsd
fi
sudo -n mkdir -p /opt/noVNC/utils
sudo -n ln -sfn /usr/share/novnc/app /opt/noVNC/app
sudo -n ln -sfn /usr/share/novnc/core /opt/noVNC/core
sudo -n ln -sfn /usr/share/novnc/include /opt/noVNC/include
sudo -n ln -sfn /usr/share/novnc/vendor /opt/noVNC/vendor
sudo -n ln -sfn /usr/share/novnc/vnc.html /opt/noVNC/vnc.html
sudo -n ln -sfn /usr/share/novnc/vnc_lite.html /opt/noVNC/vnc_lite.html
sudo -n tee /opt/noVNC/utils/novnc_proxy >/dev/null <<'SH'
#!/usr/bin/env sh
set -eu
vnc="localhost:5900"
listen="6080"
web="/opt/noVNC"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --vnc) vnc="$2"; shift 2 ;;
    --listen) listen="$2"; shift 2 ;;
    --web) web="$2"; shift 2 ;;
    *) shift ;;
  esac
done
exec websockify --web "$web" "0.0.0.0:${listen}" "$vnc"
SH
sudo -n chmod +x /opt/noVNC/utils/novnc_proxy
"""
    sandbox.commands.run(command, timeout=600, request_timeout=660)


def start_desktop_stream(sandbox: Any, *, require_auth: bool = True) -> str:
    """Start the e2b-desktop noVNC stream and return its auth key."""

    install_novnc_compat(sandbox)
    sandbox.stream.start(require_auth=require_auth)
    return sandbox.stream.get_auth_key() if require_auth else ""


def should_use_desktop_stream(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "visual", False) or getattr(args, "visual_only", False))
