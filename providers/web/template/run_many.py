#!/usr/bin/env python3
"""Run multiple Web RecreationBench tasks in parallel sandboxes."""

from __future__ import annotations

import os
import sys
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[2]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from common.template_group_runner import main  # noqa: E402


if __name__ == "__main__":
    template_dir = Path(__file__).resolve().parent
    explicit_env = os.environ.get("RB_ENV_FILE", "").strip()
    env_files = (
        (Path(explicit_env),)
        if explicit_env
        else (template_dir / ".env", template_dir / ".env.web")
    )
    repo_env = Path(__file__).resolve().parents[2] / ".env.web"
    raise SystemExit(
        main(
            platform="web",
            default_group_id="web-group",
            default_mcp_provider="playwright",
            env_files=(*env_files, repo_env),
            run_one=Path(__file__).resolve().parent / "run_one.py",
        )
    )
