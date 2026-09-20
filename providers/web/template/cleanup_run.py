#!/usr/bin/env python3
"""Recover an interrupted Web controller by killing the Sandbox in its state file."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv
from e2b import Sandbox


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("state_file", type=Path)
    args = parser.parse_args()
    load_dotenv(Path(__file__).resolve().parent / ".env.web")
    state = json.loads(args.state_file.read_text())
    api = {key.lower().replace("e2b_", ""): os.environ[key] for key in ("E2B_API_KEY", "E2B_API_URL", "E2B_DOMAIN")}
    Sandbox.connect(state["sandbox_id"], **api).kill()
    args.state_file.unlink(missing_ok=True)
    print(f"killed={state['sandbox_id']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
