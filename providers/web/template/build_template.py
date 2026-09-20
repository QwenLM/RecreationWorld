#!/usr/bin/env python3
"""Build and smoke-test an FC Agent Sandbox template for Web RecreationBench."""

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
    template_env = Path(__file__).resolve().parent / ".env.web"
    repo_env = Path(__file__).resolve().parents[3] / ".env.linux"
    load_dotenv(os.environ.get("RB_ENV_FILE", template_env), override=False)
    load_dotenv(repo_env, override=False)
    image = os.environ.get("FROM_IMAGE", "").strip()
    if not image:
        raise RuntimeError("missing environment variable: FROM_IMAGE")
    name = os.environ.get("RB_WEB_TEMPLATE_NAME", f"recreationbench-web-{int(time.time())}")
    api = {key.lower().replace("e2b_", ""): required(key) for key in ("E2B_API_KEY", "E2B_API_URL", "E2B_DOMAIN")}
    build = Template.build(
        Template().from_image(image), name=name,
        cpu_count=int(os.environ.get("RB_TEMPLATE_CPU_COUNT", "8")),
        memory_mb=int(os.environ.get("RB_TEMPLATE_MEMORY_MB", "16384")),
        skip_cache=os.environ.get("RB_TEMPLATE_SKIP_CACHE", "false").lower() in {"1", "true", "yes"},
        on_build_logs=default_build_logger(), **api,
    )
    print(f"template_name={name}\ntemplate_id={build.template_id}\nbuild_id={build.build_id}")
    sandbox = Sandbox.create(template=build.template_id, timeout=900, **api)
    try:
        result = sandbox.commands.run("rb-web-verify", timeout=180)
        print(result.stdout)
        if result.exit_code != 0:
            raise RuntimeError(f"rb-web-verify failed: {result.stderr}")
    finally:
        sandbox.kill()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
