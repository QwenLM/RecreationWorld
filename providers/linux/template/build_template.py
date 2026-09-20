#!/usr/bin/env python3
"""Build an FC Agent Sandbox template from a prebuilt Linux image."""

from __future__ import annotations

import os
import time
from pathlib import Path

from dotenv import load_dotenv
from e2b import Sandbox, Template, default_build_logger


def required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"missing environment variable: {name}")
    return value


def main() -> int:
    load_dotenv()
    repo_env = Path(__file__).resolve().parents[3] / ".env.linux"
    load_dotenv(os.environ.get("RB_ENV_FILE", repo_env), override=False)

    from_image = (
        os.environ.get("FROM_IMAGE")
        or os.environ.get("E2B_DESKTOP_IMAGE")
        or ""
    ).strip()
    if not from_image:
        raise RuntimeError("missing environment variable: FROM_IMAGE or E2B_DESKTOP_IMAGE")
    template_name = os.environ.get(
        "RB_LINUX_TEMPLATE_NAME",
        os.environ.get(
            "E2B_DESKTOP_TEMPLATE",
            f"recreationbench-linux-{int(time.time())}",
        ),
    )
    cpu_count = int(
        os.environ.get("RB_TEMPLATE_CPU_COUNT")
        or os.environ.get("E2B_TEMPLATE_CPU_COUNT")
        or "8"
    )
    memory_mb = int(
        os.environ.get("RB_TEMPLATE_MEMORY_MB")
        or os.environ.get("E2B_TEMPLATE_MEMORY_MB")
        or "16384"
    )

    api_opts = {
        "api_key": required("E2B_API_KEY"),
        "api_url": required("E2B_API_URL"),
        "domain": required("E2B_DOMAIN"),
    }

    build = Template.build(
        Template().from_image(from_image),
        name=template_name,
        cpu_count=cpu_count,
        memory_mb=memory_mb,
        skip_cache=os.environ.get("RB_TEMPLATE_SKIP_CACHE", "false").lower()
        in {"1", "true", "yes"},
        on_build_logs=default_build_logger(),
        **api_opts,
    )

    print(f"template_name={template_name}")
    print(f"template_id={build.template_id}")
    print(f"build_id={build.build_id}")

    sandbox = Sandbox.create(template=build.template_id, timeout=900, **api_opts)
    try:
        result = sandbox.commands.run("rb-linux-verify", timeout=180)
        print(result.stdout)
        if result.exit_code != 0:
            raise RuntimeError(f"rb-linux-verify failed: {result.stderr}")
    finally:
        sandbox.kill()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
