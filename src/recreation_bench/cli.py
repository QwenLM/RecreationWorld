"""Command-line entry point for the RecreationBench runtime."""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path

from recreation_bench import __version__

_PIPELINE = Path("scripts") / "core" / "pipeline.py"


def find_scripts_dir(start: Path | None = None) -> Path | None:
    """The repo's ``scripts/`` directory, or None when running from a wheel.

    Walks up from this file: an editable install leaves it inside the clone, so the pipeline is a
    few levels above. ``RB_SCRIPTS_DIR`` overrides this lookup for external controllers.
    """
    override = os.environ.get("RB_SCRIPTS_DIR", "").strip()
    if override and (Path(override) / "core" / "pipeline.py").is_file():
        return Path(override)
    here = (start or Path(__file__).resolve()).resolve()
    for parent in [here, *here.parents]:
        if (parent / _PIPELINE).is_file():
            return parent / "scripts"
    return None


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)

    if argv and argv[0] in ("-V", "--version"):
        print(f"rb {__version__}")
        return 0

    # `run` is the only subcommand: everything after it belongs to the pipeline CLI, which already
    # owns --platform / --host / --model / --stage / -x. Re-declaring those here would be a
    # second contract to keep in step.
    if argv and argv[0] == "run":
        argv = argv[1:]
    elif not argv or argv[0] in ("-h", "--help"):
        print("Run and evaluate RecreationBench tasks on a user-provided target.\n")
        print("usage: rb run [pipeline options]")
        print("       rb --version\n")
        print("Run `rb run --help` for platform, target, lifecycle, and artifact options.")
        return 0

    scripts = find_scripts_dir()
    if scripts is None:
        sys.stderr.write(
            "rb: cannot find scripts/core/pipeline.py.\n"
            "The pipeline is not packaged into the wheel (it has to be shipped to remote hosts),\n"
            "so run from a clone with `uv sync`, or set RB_SCRIPTS_DIR to the\n"
            "scripts/ directory of a checkout.\n"
        )
        return 2

    sys.path.insert(0, str(scripts))
    # runpy.run_path forces argv[0] to the script path, so argparse would print
    # "pipeline.py: error: ..." and send users hunting for a file. RB_PROG names it instead.
    os.environ.setdefault("RB_PROG", "rb run")
    sys.argv = [str(scripts / "core" / "pipeline.py"), *argv]
    try:
        runpy.run_path(str(scripts / "core" / "pipeline.py"), run_name="__main__")
    except SystemExit as exc:  # the pipeline's own exit code is the answer
        return int(exc.code or 0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
