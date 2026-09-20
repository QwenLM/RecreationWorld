"""The frozen suite's task descriptor — one `instance.json` per instance.

This DESCRIBES what the released suite already ships; it does not invent a contract. Every field
below was read from
``<artifact-root>/<frozen-suite-prefix>/<platform>/<app>/instance.json``.

``scripts/common/rb_unify/instance.py`` is the PRODUCER and validator of these descriptors: it
owns ``KEYS`` and ``validate_instance()``, it runs inside the materializer and eval bridge, and it
must keep working on hosts where nothing under ``src/`` is importable. This module is the typed
READER for the other audience -- someone consuming the suite from outside. The two representations
serve different transports and must evolve together; update both sides whenever this schema
changes.

Why this lives in ``src/`` and most of rb does not: the pipeline runs on five heterogeneous remote
hosts, delivered as a release archive with ``PYTHONPATH=<root>/scripts``. Nothing pip-installs rb
there, so a module under ``src/`` is unreachable at runtime. These models are the exception
because they have NO transport coupling: they are pure data, for whoever consumes the suite or
its results from outside — which is exactly the audience an open-source release has.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

# The suite's own directory names. `ubuntu` rather than `linux`: the release names the linux tree
# for the distro, and a profile that says `linux` reads a prefix that does not exist.
Platform = Literal["ubuntu", "windows", "macos", "android", "web"]

# The RUN side uses a different word for the same platform: instance.json says "ubuntu" while
# metrics.json says "linux" (the release profiles carry BOTH -- rb_platform: linux and
# rb_unified_platform: ubuntu -- precisely because of this). Joining tasks to results on the raw
# string therefore matches nothing for that platform, so results are normalised onto the suite's
# vocabulary and the reported value is kept alongside. Neither name can simply be renamed: the
# suite's directory names are frozen in the artifact store and rb_platform selects the adapter.
PLATFORM_ALIASES = {"linux": "ubuntu"}

SCHEMA_VERSION = 1

PatchReason = Literal[
    "suppress_nondeterminism",
    "disable_network",
    "disable_update_check",
    "disable_telemetry",
    "build_fix",
]


class TaskPatch(BaseModel):
    """One source patch declared by a released task descriptor."""

    model_config = ConfigDict(extra="forbid")

    path: str
    reason: PatchReason
    note: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def normalize_platform(name: str) -> str:
    """Map a run-side platform name onto the suite's directory vocabulary."""
    return PLATFORM_ALIASES.get((name or "").strip().lower(), (name or "").strip().lower())


class TaskInstance(BaseModel):
    """One instance of the frozen suite.

    ``extra="forbid"`` on purpose: an unrecognised key in a descriptor means the suite moved ahead
    of this schema, and failing loudly beats silently ignoring a field that changes what the task
    is. Bump SCHEMA_VERSION when adding one.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = SCHEMA_VERSION
    # NAMESPACED, "<platform>/<app>" -- not a bare app name. macOS' collector learned this the
    # hard way: the submit variant (`macos-...-macunievalinst`) is not the release key
    # (`macos-...`), and a raw-name lookup matched nothing.
    instance_id: str
    platform: Platform
    # NULL for web, whose reference is a captured site (site.tar.gz + launch.sh) rather than
    # anything built from a revision. Optional, not "" -- absent and empty differ here.
    repo: Optional[str] = None
    commit: Optional[str] = None
    patches: list[TaskPatch] = Field(default_factory=list)
    # android only: the APK id the harness installs and launches.
    package: Optional[str] = None
    license: Optional[str] = None

    @property
    def app(self) -> str:
        """The app key, without the platform namespace."""
        return self.instance_id.split("/", 1)[-1]

    @property
    def builds_from_source(self) -> bool:
        """Whether this instance can be rebuilt from a revision.

        True for the desktop platforms and android; False for web. Distinct from whether a given
        run DOES rebuild: source-backed tracks clone at run time, while web restores a frozen
        site capture.
        """
        return bool(self.repo and self.commit)
