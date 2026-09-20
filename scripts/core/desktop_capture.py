"""Full-screen capture used to record what a stage's desktop actually looked like.

Every existing screenshot is taken *through* the desktop-control MCP bridge, which means a
screenshot only exists when that bridge already works.  When it does not -- the case worth
diagnosing -- the run leaves no picture at all: one reference app reported a 1x1 window and the
1x1 PNG it produced was rejected upstream, and reconstructing that took a 17 MB proxy log.

So this grabs the screen from the calling process instead, with no MCP involved.  What it
captures is that process's own window station and desktop, which is the point: the recreation
agent runs as a different user in its own session, and a reference window has been observed to
exist in one and not the other.  Comparing a pipeline-user shot against what the agent reported
answers a question neither can answer alone.
"""

from __future__ import annotations

import argparse
import os


def capture_desktop(path: str) -> str:
    """Save a full-screen PNG at ``path``.  Returns a one-line report and never raises.

    Diagnostics must not be able to fail a scored run: an unobservable desktop is already the
    thing being investigated, and Pillow, a headless session, or a locked workstation are all
    ways this can legitimately not work.  The report names the OS user because the same code
    runs as the pipeline user and, elsewhere, as the agent's -- and which one took the shot is
    what makes two shots comparable.
    """

    user = os.environ.get("USERNAME") or os.environ.get("USER") or "?"
    try:
        from PIL import ImageGrab

        parent = os.path.dirname(os.path.abspath(path))
        if parent:
            os.makedirs(parent, exist_ok=True)
        image = ImageGrab.grab()
        image.save(path)
        return f"{os.path.basename(path)} {image.size[0]}x{image.size[1]} as {user}"
    except Exception as exc:  # noqa: BLE001 - never fail a run over a screenshot
        return f"{os.path.basename(path)} FAILED as {user}: {exc}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="core.desktop_capture")
    parser.add_argument("output")
    args = parser.parse_args(argv)
    report = capture_desktop(args.output)
    print(report)
    # The capture monitor verifies that a non-empty PNG exists.  Keep this
    # helper fail-open so screenshot diagnostics can never fail a scored run.
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised on remote desktops
    raise SystemExit(main())
