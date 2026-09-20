#!/usr/bin/env python3
"""Upload a large unified-instance archive to an existing sandbox in chunks."""

from __future__ import annotations

import argparse
import os
import shlex
from pathlib import Path

from dotenv import load_dotenv
from e2b import Sandbox


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sandbox-id", required=True)
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--remote-root", default="/tmp/recreationbench-unified")
    parser.add_argument("--chunk-mib", type=int, default=16)
    args = parser.parse_args()

    load_dotenv(os.environ.get("RB_ENV_FILE", Path(__file__).resolve().parent / ".env.linux"))
    sandbox = Sandbox.connect(
        args.sandbox_id,
        api_key=os.environ["E2B_API_KEY"],
        api_url=os.environ["E2B_API_URL"],
        domain=os.environ["E2B_DOMAIN"],
    )

    parts_dir = "/tmp/recreationbench-unified-parts"
    remote_archive = "/tmp/recreationbench-unified.tar.gz"
    sandbox.commands.run(
        f"rm -rf {parts_dir} {args.remote_root} {remote_archive} && mkdir -p {parts_dir}",
        timeout=30,
    )

    chunk_size = args.chunk_mib * 1024 * 1024
    count = 0
    with args.archive.open("rb") as fh:
        while True:
            data = fh.read(chunk_size)
            if not data:
                break
            sandbox.files.write(
                f"{parts_dir}/part-{count:04d}",
                data,
                request_timeout=120,
            )
            print(f"uploaded_part={count} bytes={len(data)}", flush=True)
            count += 1

    join_cmd = (
        f"cat {shlex.quote(parts_dir)}/part-* > {shlex.quote(remote_archive)}"
        f" && mkdir -p {shlex.quote(args.remote_root)}"
        f" && tar -xzf {shlex.quote(remote_archive)} -C {shlex.quote(args.remote_root)}"
        f" && find {shlex.quote(args.remote_root)} -maxdepth 4 -type f | wc -l"
    )
    result = sandbox.commands.run(join_cmd, timeout=180)
    print(result.stdout)
    if result.stderr:
        print(result.stderr)
    return int(result.exit_code or 0)


if __name__ == "__main__":
    raise SystemExit(main())
