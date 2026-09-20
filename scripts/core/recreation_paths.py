"""Shared path contracts for recreation workspaces and published artifacts.

There are deliberately two path layers:

* ``WorkspacePaths`` describes the one canonical path visible to the authoring agent. Linux,
  Android, and Web use ``/workspace/recreation``; macOS uses the writable
  ``/Users/Shared/workspace/recreation``; Windows uses ``C:\\workspace\\recreation``.
* ``ArtifactLayout`` describes the task-relative shape consumed after the recreation stage.
* Agent-visible runtime logs use fixed sibling paths too; they are copied into stage storage only
  after the untrusted process boundary has been stopped and made read-only.

The canonical workspace is a real, task-name-free directory while the agent is running.  After the
agent exits, the trusted harness snapshots it into the stage-owned artifact directory.  Keeping
that boundary as a copy/move instead of a symlink or junction prevents the agent from resolving a
supposedly anonymous path back to a task-bearing storage path.  The prompt resolver intentionally
has no runtime path overrides or compatibility aliases. New artifacts use one canonical
``recreation`` inner directory; the artifact resolver does not guess old layouts.

Snapshotting that boundary means the trusted harness copies and deletes a tree the agent wrote,
so the helpers for doing that safely -- the extended-length spelling, reparse-point detection,
and the rmtree unlock handler -- live here with the paths they apply to rather than inside one
platform's stage.
"""

from __future__ import annotations

import ntpath
import os
import posixpath
import stat
from collections.abc import Callable, Mapping
from dataclasses import dataclass

CANONICAL_INNER = "recreation"
POSIX_WORKSPACE_ROOT = "/workspace/recreation"
MACOS_WORKSPACE_ROOT = "/Users/Shared/workspace/recreation"
WINDOWS_WORKSPACE_ROOT = r"C:\workspace\recreation"
WINDOWS_RUNTIME_ROOT = r"C:\workspace\runtime"


@dataclass(frozen=True)
class ArtifactLayout:
    """Stable, task-relative artifact facts for one platform."""

    kind: str
    stage_dir: str
    inner_roots: tuple[str, ...]
    markers: tuple[str, ...]
    build_script: str | None = None
    launch_script: str | None = None
    entry: str | None = None


ARTIFACT_LAYOUTS: dict[str, ArtifactLayout] = {
    "ubuntu": ArtifactLayout(
        kind="source_build",
        stage_dir=CANONICAL_INNER,
        inner_roots=(CANONICAL_INNER,),
        markers=("build.sh", "launch.sh", "src", "bin"),
        build_script="build.sh",
        launch_script="launch.sh",
    ),
    "linux": ArtifactLayout(
        kind="source_build",
        stage_dir=CANONICAL_INNER,
        inner_roots=(CANONICAL_INNER,),
        markers=("build.sh", "launch.sh", "src", "bin"),
        build_script="build.sh",
        launch_script="launch.sh",
    ),
    "macos": ArtifactLayout(
        kind="source_build",
        stage_dir=CANONICAL_INNER,
        inner_roots=(CANONICAL_INNER,),
        markers=("build.sh", "launch.sh", "src", "build"),
        build_script="build.sh",
        launch_script="launch.sh",
    ),
    "windows": ArtifactLayout(
        kind="source_build",
        stage_dir=CANONICAL_INNER,
        inner_roots=(CANONICAL_INNER,),
        markers=("build.ps1", "launch.ps1", "src", "bin", "build_result.json"),
        build_script="build.ps1",
        launch_script="launch.ps1",
    ),
    "android": ArtifactLayout(
        kind="apk",
        stage_dir=CANONICAL_INNER,
        inner_roots=(CANONICAL_INNER,),
        markers=("recreated.apk",),
        entry="recreated.apk",
    ),
    "web": ArtifactLayout(
        kind="site",
        stage_dir=".",
        inner_roots=(CANONICAL_INNER,),
        markers=("src", "public", "package.json"),
        entry="output/index.html",
    ),
}


@dataclass(frozen=True)
class WorkspacePaths:
    """Paths shown to an agent while it authors a recreation."""

    root: str
    source_dir: str
    artifact_path: str
    build_script: str = ""
    launch_script: str = ""

    def substitutions(self) -> dict[str, str]:
        return {
            "workspace_dir": self.root,
            "source_dir": self.source_dir,
            "artifact_path": self.artifact_path,
            "build_script": self.build_script,
            "launch_script": self.launch_script,
        }


class WorkspacePathError(ValueError):
    """A platform's workspace path cannot be derived from its runtime values."""


def _posix_join(root: str, child: str) -> str:
    return posixpath.join(root.rstrip("/"), child)


def _windows_join(root: str, child: str) -> str:
    return ntpath.join(root.rstrip("\\/"), child)


def workspace_paths(platform: str, runtime: Mapping[str, object]) -> WorkspacePaths:
    """Resolve the one prompt-facing workspace contract for ``platform``.

    ``runtime`` remains in the API because prompt rendering also receives platform values such as
    the device ID and site URL. It cannot override filesystem paths: allowing that would make the
    prompt and the process cwd silently diverge again.
    """

    del runtime

    selected = (platform or "").lower()
    layout = ARTIFACT_LAYOUTS.get(selected)
    if layout is None:
        raise WorkspacePathError(f"unsupported platform {platform!r}")

    if selected in ("linux", "ubuntu"):
        root = POSIX_WORKSPACE_ROOT
        return WorkspacePaths(
            root=root,
            source_dir=_posix_join(root, "src"),
            build_script=_posix_join(root, layout.build_script or "build.sh"),
            launch_script=_posix_join(root, layout.launch_script or "launch.sh"),
            artifact_path=_posix_join(root, "bin"),
        )

    if selected == "macos":
        root = MACOS_WORKSPACE_ROOT
        return WorkspacePaths(
            root=root,
            source_dir=root,
            build_script=_posix_join(root, layout.build_script or "build.sh"),
            launch_script=_posix_join(root, layout.launch_script or "launch.sh"),
            artifact_path=_posix_join(root, "build"),
        )

    if selected == "windows":
        root = WINDOWS_WORKSPACE_ROOT
        return WorkspacePaths(
            root=root,
            source_dir=_windows_join(root, "src"),
            build_script=_windows_join(root, layout.build_script or "build.ps1"),
            launch_script=_windows_join(root, layout.launch_script or "launch.ps1"),
            artifact_path=_windows_join(root, "bin"),
        )

    if selected == "android":
        root = POSIX_WORKSPACE_ROOT
        return WorkspacePaths(
            root=root,
            source_dir=_posix_join(root, "src"),
            artifact_path=_posix_join(root, layout.entry or "recreated.apk"),
        )

    root = POSIX_WORKSPACE_ROOT
    return WorkspacePaths(root, root, _posix_join(root, "output/index.html"))


def extended_length_path(path: str) -> str:
    """The ``\\\\?\\`` spelling of an already-absolute Windows path.  Pure, and idempotent.

    A recreation's own build output nests far deeper than anything the harness authors, and the
    260-character limit is a property of the path, not of the platform doing the copying -- so the
    spelling lives with the other path contracts rather than inside one platform's stage.  The
    caller decides whether it applies (``os.name == "nt"``) and passes an absolute path: the
    prefix disables normalisation, so a relative or forward-slash path would not resolve.
    """

    if path.startswith("\\\\?\\"):
        return path
    if path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + path[2:]
    return "\\\\?\\" + path


def is_reparse_point(path: str) -> bool:
    """True for a symlink, a Windows junction, or any other reparse point.

    An agent can create a junction inside its own workspace without any privilege it was not
    already given, so every harness operation that walks or deletes a recreation tree has to be
    able to recognise one before following it.  ``os.path.islink`` alone does not: a junction is
    a reparse point but not a symlink, and ``os.path.isjunction`` only exists on 3.12+.
    """

    if os.path.islink(path):
        return True
    if getattr(os.path, "isjunction", lambda _path: False)(path):
        return True
    try:
        attrs = os.lstat(path).st_file_attributes
    except (AttributeError, OSError):
        return False
    return bool(attrs & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def force_writable_onerror(
    func: Callable[[str], object], path: str, _exc: object = None
) -> None:
    """``shutil.rmtree`` handler: unlock exactly the entry that blocked the delete, then retry.

    Windows refuses to unlink a read-only file, and an agent that ran ``git init`` leaves
    ``.git\\objects`` read-only, so a first pass dies on a tree that is otherwise free.

    Only the entry rmtree actually failed on is touched.  Clearing the tree up front instead --
    ``os.walk`` plus ``chmod`` -- also reached every junction the agent had planted, and because
    ``chmod`` follows a link that added the write bit to a target *outside* the workspace: an
    agent-controlled path modified with harness privilege the agent does not hold.  So a reparse
    point is re-raised rather than modified, and the mode is read with ``lstat`` so it cannot be
    taken from a link's target either.

    Re-raising when the entry cannot be unlocked keeps callers that need an empty path strict: a
    genuinely locked file, a just-built exe still held open, must still fail.  Called only from
    rmtree's ``except`` block, where a bare ``raise`` re-raises the original error; that is the
    contract for both the 3.12 ``onexc`` spelling and the older ``onerror`` one, whose three
    arguments are otherwise the same.
    """

    if is_reparse_point(path):
        raise
    try:
        os.chmod(path, os.lstat(path).st_mode | stat.S_IWRITE)
    except OSError:
        raise
    func(path)


def artifact_stage_dirs(platform: str, dir_suffix: str = "") -> list[str]:
    """The current task-relative stage directory for a platform/run."""

    selected = (platform or "").lower()
    layout = ARTIFACT_LAYOUTS.get(selected)
    if layout is None:
        return []
    if selected == "windows" and dir_suffix:
        return [f"recreation_{dir_suffix}"]
    return [layout.stage_dir]
