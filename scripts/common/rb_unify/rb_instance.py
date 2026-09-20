"""``RBInstance`` — one released RB task as an object.

:mod:`schema` models the records that flow THROUGH an evaluation (RBRecord / RBAudit /
VLMAssertion). This module models the artifact that SITS ON DISK, which is a different
level and had no type at all: validation was spread over three modules
(:mod:`instance` for the descriptor, :mod:`reference` for the reference shape,
:mod:`release` for component presence) and every caller poked at raw dicts.

The four-component contract::

    <instance_id>/
      instance.json         identity (the only JSON descriptor)
      reference/            launch.sh [+ build.sh, patches/, screenshots/, web archive]
      tests/                the scoring suite + kit + conftest.py
      vlm_assertions.json   VLM assertions, schema v2 — never in the scoring denominator

Two things this type makes structural rather than incidental:

* **The audit is computed, never read.** ``rb_audit.json`` is not a component: ~20% of
  it was aggregates that are pure functions of ``records[]``, and ``records[]`` is
  recoverable from ``tests/`` (measured against the audits it replaced: macOS, windows
  and web 100% of names and depth, ubuntu 100%/97%, android 69-100% per app with the
  residue reported). :meth:`RBInstance.audit` derives, so there is no read path that
  can drift from the suite.
* **Every problem names its component.** A bare list of strings cannot say whether a
  release failed on identity or on the reference, which is the first thing anyone
  asks. Hence :class:`Problem`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

COMPONENTS = ("descriptor", "reference", "tests", "vlm")

DESCRIPTOR = "instance.json"
VLM_FILE = "vlm_assertions.json"
REFERENCE_DIR = "reference"
TESTS_DIR = "tests"


@dataclass(frozen=True)
class Problem:
    """One release-blocking defect, attributed to the component that owns it."""

    component: str
    message: str

    def __str__(self) -> str:  # pragma: no cover - display only
        return f"{self.component}: {self.message}"


@dataclass
class RBInstance:
    """A released task directory. Construct with :meth:`load`, not directly."""

    path: Path
    descriptor: dict = field(default_factory=dict)
    load_error: str | None = None

    # ---------------------------------------------------------------- loading
    @classmethod
    def load(cls, path: str | Path) -> RBInstance:
        """Read an instance directory.

        A missing or broken descriptor is RECORDED, not raised: a scan over a release
        tree has to report the bad instance alongside the good ones instead of
        aborting on the first one.
        """
        p = Path(path)
        f = p / DESCRIPTOR
        if not f.is_file():
            return cls(path=p, load_error=f"{DESCRIPTOR} missing")
        try:
            data = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            return cls(path=p, load_error=f"{DESCRIPTOR} unreadable: {exc}")
        if not isinstance(data, dict):
            return cls(path=p, load_error=f"{DESCRIPTOR} is not an object")
        return cls(path=p, descriptor=data)

    @classmethod
    def scan(cls, root: str | Path) -> list[RBInstance]:
        """Every instance under a release tree.

        Accepts ``<root>/<app>/`` and ``<root>/<platform>/<app>/`` — the packager
        writes the latter, a single-platform run the former.
        """
        r = Path(root)
        seen: set = set()
        out: list[RBInstance] = []
        for f in sorted(r.glob(f"*/{DESCRIPTOR}")) + sorted(
            r.glob(f"*/*/{DESCRIPTOR}")
        ):
            if f.parent in seen:
                continue
            seen.add(f.parent)
            out.append(cls.load(f.parent))
        return out

    # ------------------------------------------------------------- identity
    @property
    def platform(self) -> str:
        return str(self.descriptor.get("platform") or "")

    @property
    def instance_id(self) -> str:
        return str(self.descriptor.get("instance_id") or self.path.name)

    @property
    def repo(self) -> str | None:
        return self.descriptor.get("repo")

    @property
    def commit(self) -> str | None:
        return self.descriptor.get("commit")

    @property
    def package(self) -> str | None:
        return self.descriptor.get("package")

    @property
    def license(self) -> str | None:
        return self.descriptor.get("license")

    @property
    def patches(self) -> list:
        p = self.descriptor.get("patches")
        return p if isinstance(p, list) else []

    # ---------------------------------------------------------- component paths
    @property
    def reference_dir(self) -> Path:
        return self.path / REFERENCE_DIR

    @property
    def tests_dir(self) -> Path:
        return self.path / TESTS_DIR

    @property
    def vlm_path(self) -> Path:
        return self.path / VLM_FILE

    @property
    def archive(self) -> Path:
        from .reference import ARCHIVE

        return self.reference_dir / ARCHIVE

    # ------------------------------------------------------------- validation
    def validate(self) -> list[Problem]:
        """All four components in one call, each problem tagged with its component."""
        from .instance import validate_instance
        from .reference import validate_reference

        if self.load_error:
            return [Problem("descriptor", self.load_error)]

        probs = [Problem("descriptor", m) for m in validate_instance(self.descriptor)]
        probs += [
            Problem("reference", m)
            for m in validate_reference(
                str(self.reference_dir), self.platform, patches=self.patches
            )
        ]

        if not self.tests_dir.is_dir():
            probs.append(Problem("tests", f"{TESTS_DIR}/ missing"))
        else:
            suites = list(self.tests_dir.rglob("test_*.py")) + list(
                self.tests_dir.rglob("*.spec.ts")
            )
            if not suites:
                probs.append(Problem("tests", "no test files"))
            # conftest.py is what registers the shared plugin. Without it the files are
            # present but the suite cannot emit the canonical records — android ships
            # in exactly this state today.
            if self.platform != "web" and not list(self.tests_dir.rglob("conftest.py")):
                probs.append(
                    Problem("tests", "conftest.py missing (shared plugin unregistered)")
                )

            # A frozen manifest whose scored set is EMPTY ships a suite that cannot
            # score: every test was baseline-failing, so the eval reports 0 of 0 and
            # finalize_scores yields None (dropped from the averages) rather than a
            # number. Presence alone therefore overstates the release — measured: 3 of
            # 185 pytest instances are in exactly this state, with 1-55 ignored tests
            # and nothing scored.
            mf = self.tests_dir / "test_manifest.json"
            if mf.is_file():
                try:
                    man = json.loads(mf.read_text())
                except (OSError, json.JSONDecodeError) as exc:
                    probs.append(
                        Problem("tests", f"test_manifest.json unreadable: {exc}")
                    )
                else:
                    if not man.get("tests"):
                        probs.append(
                            Problem(
                                "tests",
                                "frozen scored set is empty "
                                f"({len(man.get('ignored') or [])} baseline-failing) — "
                                "nothing to score",
                            )
                        )

        if not self.vlm_path.is_file():
            probs.append(Problem("vlm", f"{VLM_FILE} missing"))
        return probs

    @property
    def releasable(self) -> bool:
        # vlm_assertions is advisory, never a release gate: the schema keeps it OUT of
        # the scoring denominator, and web + windows generate their assertions at judge
        # time (their frozen data carries only judge config / screenshot names, no
        # statements) rather than freezing them. A missing vlm file is still reported by
        # validate()/problems_by_component() as an advisory, but does not block release.
        return not [p for p in self.validate() if p.component != "vlm"]

    def problems_by_component(self) -> dict:
        out: dict = {c: [] for c in COMPONENTS}
        for p in self.validate():
            out.setdefault(p.component, []).append(p.message)
        return {k: v for k, v in out.items() if v}

    def vlm(self) -> dict:
        if not self.vlm_path.is_file():
            return {}
        try:
            return json.loads(self.vlm_path.read_text())
        except (OSError, json.JSONDecodeError):
            return {}
