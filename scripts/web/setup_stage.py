#!/usr/bin/env python3
"""Establish and ATTEST the web agent's file boundary. A thin adapter over core.permission_probe.

Everything that used to live here -- planting stand-ins, the positive control, existence-aware
probes, the writable probe, the cache check, the verdict -- now lives in
``scripts/core/permission_probe.py``, because four copies produced four versions of the same three
bugs. What is web-specific is only this:

* the PATHS. ``run_agent.py`` serves ``<task_dir>/site`` over localhost and asks the agent to
  rebuild what it SEES, so nothing about the task needs those files readable; and on this platform
  the tests ship inside the same payload, so one protected path covers reference and answer key.
* the fact that this lane ALREADY demotes, to ``agent``, via
  ``setpriv --reuid=agent --regid=agent --clear-groups``. core.permission_probe.PLATFORMS records
  that, so the boundary is attested against the principal production actually uses.

Web has no stage concept — the lane is a single-phase runner — so this stays a standalone entrypoint
rather than a dispatch case.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from core import permission_probe  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--site-dir", required=True)
    ap.add_argument("--workspace-dir", required=True)
    ap.add_argument("--user", default="")
    ap.add_argument(
        "--work-dir", default=(os.environ.get("TMPDIR") or "/tmp") + "/rb_permission"
    )
    ap.add_argument("--report")
    args = ap.parse_args(argv)

    work = pathlib.Path(args.work_dir)
    work.mkdir(parents=True, exist_ok=True)
    report = args.report or str(work / "report.json")
    return permission_probe.main(
        [
            "--platform",
            "web",
            "--run-id",
            args.run_id,
            "--protected",
            args.site_dir,
            "--writable",
            args.workspace_dir,
            "--spec-out",
            str(work / "spec.json"),
            "--report",
            report,
            "--env-out",
            str(work / "permission_env.sh"),
            *(["--user", args.user] if args.user else []),
        ]
    )


if __name__ == "__main__":
    raise SystemExit(main())
