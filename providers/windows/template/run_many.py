#!/usr/bin/env python3
"""Run multiple Windows tasks in independent Sandbox instances."""

from __future__ import annotations

import os
import sys
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SERVICE_ROOT))

from common.template_group_runner import main  # noqa: E402


if __name__ == "__main__":
    here = Path(__file__).resolve().parent
    # Windows children read the released directory directly; archive packing
    # is unnecessary because the controller transfers each task over SFTP.
    local_dir = os.environ.get("RB_UNIFIED_LOCAL_DIR", "").strip()
    if local_dir and not os.environ.get("RB_UNIFIED_CACHE_DIR", "").strip():
        os.environ["RB_UNIFIED_CACHE_DIR"] = local_dir
    has_cache_flag = "--unified-cache-dir" in sys.argv
    if (local_dir or has_cache_flag) and "--no-prefetch-unified" not in sys.argv:
        sys.argv.insert(1, "--no-prefetch-unified")
    raise SystemExit(main(
        platform="windows",
        default_group_id="windows-group",
        default_mcp_provider="cua-driver",
        env_files=(Path(os.environ.get("RB_ENV_FILE", here / ".env.windows")),),
        run_one=here / "run_one.py",
    ))
