#!/usr/bin/env python3
"""Python operations used by the Linux cross-user AT-SPI diagnostic."""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from pathlib import Path


def create_window(title: str, label: str, seconds: float) -> None:
    import gi

    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk

    window = Gtk.Window(title=title)
    window.add(Gtk.Button(label=label))
    window.show_all()
    threading.Timer(seconds, Gtk.main_quit).start()
    Gtk.main()


def list_apps(delay: float) -> None:
    import gi

    gi.require_version("Atspi", "2.0")
    from gi.repository import Atspi

    Atspi.init()
    time.sleep(delay)
    desktop = Atspi.get_desktop(0)
    count = desktop.get_child_count()
    print(f"total_apps={count}")
    for index in range(count):
        app = desktop.get_child_at_index(index)
        if app:
            print(f"app={app.get_name()}")


def find_window() -> None:
    payload = json.load(sys.stdin)
    for window in payload.get("windows", []):
        title = window.get("title") or ""
        if "CROSSUSER" in title or "SAMEUSER" in title:
            print(window["pid"], window["window_id"])


def summarize_cua(path: Path) -> None:
    payload = json.loads(path.read_text())
    print(f"cua_cli_ec={payload.get('element_count', 0)}")
    tree = str(payload.get("tree_markdown", ""))[:150].replace("\n", " ")
    print(f"cua_tree={tree}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    window = commands.add_parser("create-window")
    window.add_argument("--title", required=True)
    window.add_argument("--label", required=True)
    window.add_argument("--seconds", type=float, default=20)
    apps = commands.add_parser("list-apps")
    apps.add_argument("--delay", type=float, default=1)
    commands.add_parser("find-window")
    summary = commands.add_parser("summarize-cua")
    summary.add_argument("path", type=Path)
    args = parser.parse_args()

    if args.command == "create-window":
        create_window(args.title, args.label, args.seconds)
    elif args.command == "list-apps":
        list_apps(args.delay)
    elif args.command == "find-window":
        find_window()
    else:
        summarize_cua(args.path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
