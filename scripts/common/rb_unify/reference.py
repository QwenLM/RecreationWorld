"""The reference/ contract: a directory shape, not a descriptor file.

There is deliberately **no reference.json**. Of the six fields an earlier draft put in
one, four were derivable straight from the directory listing (``build`` / ``launch`` /
``screenshots`` / ``schema_version``) and the remaining two were dropped with the
integrity sidecars, so the file carried nothing a ``ls`` could not answer.

    reference/
      build.sh             desktop only: reproducible build
      launch.sh            all five: bring the app under test up
      patches/*.patch      optional source patches; ordered by instance.patches
      screenshots/         optional: provenance for the VLM assertions
      reference.tar.gz     web only: the frozen site mirror

The ``wrapsource`` release stores source identity as ``repo@commit`` in
``instance.json`` rather than duplicating source archives. Desktop and Android
runtimes clone that revision and apply declared patches. Web has no source revision,
so its frozen site remains in ``reference.tar.gz``.

Measured state that shaped this:

* ubuntu's build knowledge **was never captured**: ``scripts/linux/stages/build.py``
  ships a BUILD_PROMPT and an API key and lets an agent work the build out per run, so
  build.sh has to be authored (or captured from a successful run), not copied.
* macOS is the only track that already ships build.sh + launch.sh.
* windows' conftest already reads ``RB_APP_BINARY`` / ``RB_LAUNCH_SCRIPT``, so its
  contract is in place and only the scripts are missing.
* Android is built from its pinned source in a trusted staging directory before the
  recreation agent starts; that source and the resulting on-disk APK are then removed
  from the agent-visible filesystem.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

ARCHIVE = "reference.tar.gz"
LAUNCH = "launch.sh"
BUILD = "build.sh"

# windows' single entry point is PowerShell, not bash: its launcher and build recipe
# are .ps1 and carry no unix execute bit. The reference contract is otherwise identical
# (one archive, one launcher, desktop needs a build recipe).
LAUNCH_BY_PLATFORM = {"windows": "launch.ps1"}
BUILD_BY_PLATFORM = {"windows": "build.ps1"}


def launch_name(platform: str) -> str:
    return LAUNCH_BY_PLATFORM.get(platform, LAUNCH)


def build_name(platform: str) -> str:
    return BUILD_BY_PLATFORM.get(platform, BUILD)


PLATFORMS = ("ubuntu", "macos", "windows", "android", "web")
# platforms whose reference must be BUILT from source, hence need a build recipe
NEEDS_BUILD = ("ubuntu", "macos", "windows")

# What an optional legacy archive holds. Only web requires one in wrapsource.
ARCHIVE_CONTENT = {
    "ubuntu": "source tree at repo@commit",
    "macos": "source tree at repo@commit",
    "windows": "source tree at repo@commit",
    "android": "the reference APK",
    "web": "the frozen site mirror",
}
NEEDS_ARCHIVE = ("web",)

# The launch interface. A test never learns where the app came from; only the runner
# does, and only through these — which is what lets ONE suite score both the
# reference and a recreation.
ENV = {
    "RB_LAUNCH_SCRIPT": "the launch script (single entry point, all platforms)",
    "RB_APP_BINARY": "absolute path of the built executable (desktop, from build.sh)",
    "RB_APP_OUTPUT_DIR": "where build.sh put its artifact (macOS APP_OUTPUT_DIR)",
    "RB_PACKAGE": "applicationId to launch (android; taken from instance.json)",
    "RB_BASE_URL": "origin the site is served at (web; announced by launch.sh)",
    "RB_RESULTS_DIR": "where the runner writes its audit output",
}


def validate_reference(ref_dir: str, platform: str, *, patches: list | None = None) -> list[str]:
    """Validate the SHAPE of a reference/ directory. Empty list means it is usable.

    Checks the files represented by the object-store release rather than parsing a
    second manifest. Object stores do not preserve Unix executable bits, so runtimes
    restore script modes after download.
    """
    d = Path(ref_dir)
    errs: list[str] = []
    if platform not in PLATFORMS:
        return [f"unknown platform {platform!r}"]
    if not d.is_dir():
        return [f"reference/ missing: {ref_dir}"]

    arch = d / ARCHIVE
    if platform in NEEDS_ARCHIVE and not arch.is_file():
        errs.append(f"{ARCHIVE} missing ({ARCHIVE_CONTENT[platform]})")
    elif arch.exists() and (not arch.is_file() or arch.stat().st_size == 0):
        errs.append(f"{ARCHIVE} is empty or not a regular file")

    launch_fn = launch_name(platform)
    launch = d / launch_fn
    if not launch.is_file():
        errs.append(f"{launch_fn} missing (required on every platform)")
    elif launch.stat().st_size == 0:
        errs.append(f"{launch_fn} is empty")

    build_fn = build_name(platform)
    build = d / build_fn
    if platform in NEEDS_BUILD:
        if not build.is_file():
            errs.append(f"{build_fn} missing ({platform} builds its reference from source)")
        elif build.stat().st_size == 0:
            errs.append(f"{build_fn} is empty")
    elif build.exists():
        errs.append(f"{build_fn} must not exist on {platform} (nothing is built)")

    # Every shipped patch must be referenced by instance.patches, in order: an
    # unreferenced .patch would never be applied and would silently change nothing.
    shipped = (
        sorted(p.name for p in (d / "patches").glob("*.patch")) if (d / "patches").is_dir() else []
    )
    referenced = [Path(str(p.get("path", ""))).name for p in (patches or []) if isinstance(p, dict)]
    for name in shipped:
        if name not in referenced:
            errs.append(f"patches/{name} is not referenced by instance.patches")
    for name in referenced:
        if name and name not in shipped:
            errs.append(f"instance.patches references patches/{name}, which is not shipped")
    for item in patches or []:
        if not isinstance(item, dict) or not item.get("path") or not item.get("sha256"):
            continue
        patch = d.parent / str(item["path"])
        if patch.is_file():
            actual = hashlib.sha256(patch.read_bytes()).hexdigest()
            if actual != item["sha256"]:
                errs.append(
                    f"{item['path']} sha256 mismatch: expected {item['sha256']}, got {actual}"
                )

    # Things that must NOT be here: generator input (gt/) and generator output.
    # gt/ was measured to be read by ZERO tests — its values are inlined into the
    # test source at generation time — and baseline_results/ is the kit's own output.
    for stale in ("gt", "baseline_results", "reference.json"):
        if (d / stale).exists():
            errs.append(f"{stale} must not ship (generator input/output, not a scoring input)")

    return errs
