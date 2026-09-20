#!/usr/bin/env python3
"""Exit successfully when the requested Python module can be imported."""

from __future__ import annotations

import importlib
import sys


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} MODULE", file=sys.stderr)
        return 2
    importlib.import_module(sys.argv[1])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
