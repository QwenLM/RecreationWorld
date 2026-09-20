#!/usr/bin/env python3
"""Report whether PyObjC can enumerate applications in the macOS GUI session."""

from __future__ import annotations


def main() -> None:
    try:
        from AppKit import NSWorkspace

        apps = NSWorkspace.sharedWorkspace().runningApplications()
        print(f"OK: {len(apps)} apps found")
        for app in apps[:5]:
            print(f"  - {app.localizedName()} (pid={app.processIdentifier()})")
    except Exception as exc:  # noqa: BLE001 - this is a best-effort diagnostic
        print(f"FAILED: {exc}")


if __name__ == "__main__":
    main()
