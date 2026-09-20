#!/usr/bin/env python3
"""Report whether a fresh network namespace can still reach public addresses."""

from __future__ import annotations

import socket

PROBE_IPS = ("1.1.1.1", "8.8.8.8")


def main() -> int:
    for ip in PROBE_IPS:
        try:
            socket.create_connection((ip, 443), timeout=3).close()
        except OSError:
            continue
        print(f"LEAK {ip}")
        return 0
    print("SEALED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
