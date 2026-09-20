#!/usr/bin/env python3
"""Where the RECREATED app is, answered once for all five platforms.

The recreated app is the model's actual output — the thing being scored. New artifacts have one
canonical layout per platform, with the app rooted at ``recreation`` wherever a stage directory
exists:

    linux     <task>/recreation/recreation/               src/ bin/ build.sh launch.sh
    windows   <task>/recreation[_suffix]/recreation/      src/ bin/ build.ps1 launch.ps1
    android   <task>/recreation/recreation/               recreated.apk
    web       <task>/recreation/                          src/ public/ output/index.html
    macos     inside <task>/<task>_<model>/results.tar.gz source + build/<name>.app

The outer stage keys remain platform pipeline concerns. This module owns the canonical artifact
shape inside them:

  * every platform writes a small ``recreation_manifest.json`` at ONE canonical key next to its
    artifacts, saying where its recreated app root is and what shape it has;
  * ``resolve()`` reads that manifest when present and otherwise checks only the canonical root.

Same shape as ``rb_unify.eval_bridge.unified_app_candidates``, which already solved the sibling
problem (one key derivation instead of a private copy per platform).

STDLIB ONLY: android's kit is vendored onto the emulator host with no pip available.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from core.recreation_paths import (
    ARTIFACT_LAYOUTS,
    artifact_stage_dirs,
)
from core.recreation_paths import (
    CANONICAL_INNER as _CANONICAL_INNER,
)

# Public for VM-side platform code that already imports the artifact contract as one module.
CANONICAL_INNER = _CANONICAL_INNER

MANIFEST_NAME = "recreation_manifest.json"
SCHEMA_VERSION = 1

# Compatibility exports for callers that imported the old dictionaries. Their values now come
# from the same structured contract used by the prompt renderer.
KINDS = tuple(dict.fromkeys(layout.kind for layout in ARTIFACT_LAYOUTS.values()))
PLATFORM_KIND = {name: layout.kind for name, layout in ARTIFACT_LAYOUTS.items()}
PLATFORM_SCRIPTS = {
    name: (layout.build_script, layout.launch_script)
    for name, layout in ARTIFACT_LAYOUTS.items()
}


def artifact_roots(platform: str, dir_suffix: str = "") -> list[str]:
    """Canonical task-relative root for the recreated app."""
    p = (platform or "").lower()
    if p == "macos":
        # macOS ships the recreation inside results.tar.gz rather than as a directory, so there
        # is no root to point at -- the manifest records the archive member instead.
        return []
    if p not in PLATFORM_INNER:
        # An UNKNOWN platform gets no candidates, not the default stage dir: a guess is exactly
        # what this module exists to replace, and a wrong guess resolves to a directory that is
        # not the app while reporting success.
        return []
    out: list[str] = []

    def _add(cand: str) -> None:
        # "." is not a path component: web has no stage dir, so joining naively produced
        # "./recreation", which no artifact store key or string comparison would match.
        cand = "/".join(x for x in cand.split("/") if x not in ("", "."))
        if cand and cand not in out:
            out.append(cand)

    for stage in stage_dirs(p, dir_suffix):
        for inner in PLATFORM_INNER.get(p, ()):
            _add(f"{stage}/{inner}")
    return out


def stage_dirs(platform: str, dir_suffix: str = "") -> list[str]:
    """The stage directory a platform's artifacts live under, inside the task directory.

    web has none: its delivered dir IS the task-relative `recreation/`, so the stage dir is the
    task dir itself ("."). A suffixed Windows run has its own explicit current stage key.
    """
    return artifact_stage_dirs(platform, dir_suffix)


# The stage dir holds HARNESS output (trajectory.jsonl, prompt.txt, sessions/), so the app goes in
# a named subdir. The canonical writer uses ``recreation`` everywhere a stage directory exists:
#
#     linux/ubuntu  <stage>/recreation/  src/ bin/ build.sh launch.sh   (confirmed, keeweb run)
#     windows       <stage>/recreation/  src/ bin/ build.ps1 launch.ps1
#     android       <stage>/recreation/  recreated.apk
#     web           <stage>/recreation/  src/ public/ package.json
#
# The stage root is deliberately not a candidate.
PLATFORM_INNER = {name: layout.inner_roots for name, layout in ARTIFACT_LAYOUTS.items()}


def inner_roots(platform: str) -> list[str]:
    """The canonical stage-dir-relative root for the recreated app."""
    p = (platform or "").lower()
    return [r for r in PLATFORM_INNER.get(p, ()) if r]


# What proves a directory really holds a recreation. Owned here so callers do not each restate the
# list -- a caller with a shorter list silently resolves to a different depth than its neighbours.
PLATFORM_MARKERS = {name: layout.markers for name, layout in ARTIFACT_LAYOUTS.items()}


def _has_marker(cand: Path, name: str) -> bool:
    """A marker counts only if it holds something.

    Empty ``src/`` and ``bin/`` directories may be harness scaffolding, so existence alone cannot
    prove that the canonical directory contains an authored recreation.
    """
    p = cand / name
    if p.is_dir():
        try:
            return any(p.iterdir())
        except OSError:
            return False
    try:
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False


def app_root(stage_dir, platform: str, *, markers=None) -> str:
    """The recreated app's canonical directory inside ``stage_dir``.

    ``markers`` names files that identify a real recreation (for example ``build.ps1`` or ``src``).
    A stage-local manifest is accepted only when it names the same canonical directory.
    """
    base = Path(stage_dir)
    mf = base / MANIFEST_NAME
    if mf.is_file():
        try:
            m = json.loads(mf.read_text())
            if not isinstance(m, dict):
                raise ValueError("manifest must be an object")
            actual_platform = str(m.get("platform") or "").lower()
            valid = (
                m.get("schema_version") == SCHEMA_VERSION
                and _same_platform((platform or "").lower(), actual_platform)
                and m.get("kind") == PLATFORM_KIND.get(actual_platform)
            )
            root = str(m.get("root") or ".") if valid else ""
            cand = base if root == "." else _contained(base, root)
            layout = ARTIFACT_LAYOUTS.get((platform or "").lower())
            canonical = (
                base / layout.inner_roots[0]
                if layout and layout.inner_roots and (platform or "").lower() != "macos"
                else base
            )
            if (
                valid
                and cand is not None
                and cand.resolve() == canonical.resolve()
                and cand.is_dir()
            ):
                return str(cand)
        except (AttributeError, OSError, ValueError):
            pass
    marks = tuple(
        markers
        if markers is not None
        else PLATFORM_MARKERS.get((platform or "").lower(), ())
    )
    for rel in inner_roots(platform):
        cand = base if rel == "." else base / rel
        if not cand.is_dir():
            continue
        if not marks or any(_has_marker(cand, m) for m in marks):
            return str(cand)
    # Nothing matched a marker: return the canonical path so the caller reports a missing artifact
    # there rather than at the stage root or whichever legacy depth happened to exist.
    layout = ARTIFACT_LAYOUTS.get((platform or "").lower())
    if layout and layout.inner_roots:
        return str(base / layout.inner_roots[0])
    return str(base)


def manifest(
    platform: str,
    root: str,
    *,
    kind: str = "",
    build: str = "",
    launch: str = "",
    archive: str = "",
    entry: str = "",
) -> dict:
    """The canonical descriptor. ``root`` is relative to the task's artifact dir.

    ``archive`` is for the one platform that ships the recreation inside a tarball: ``root`` is
    then the path INSIDE that archive. Empty strings are dropped rather than stored, so a
    consumer can tell "not applicable" from "present but blank" -- the same honesty rule
    instance.json enforces.
    """
    p = (platform or "").lower()
    layout = ARTIFACT_LAYOUTS.get(p)
    build_script, launch_script = PLATFORM_SCRIPTS.get(p, (None, None))
    out = {
        "schema_version": SCHEMA_VERSION,
        "platform": p,
        "kind": kind or PLATFORM_KIND.get(p, ""),
        "root": str(root).strip("/"),
        "build": build or (build_script or ""),
        "launch": launch or (launch_script or ""),
        "archive": archive,
        "entry": entry or ((layout.entry or "") if layout else ""),
    }
    return {k: v for k, v in out.items() if v != ""}


# --- the recreation -> eval boundary ---------------------------------------------------------
# §7: "Eval 只消费明确声明的任务产物". The manifest IS that declaration, and once the release scope
# is recreation+eval it is the ONLY handover between them. So a manifest that names a file which
# does not exist is not a cosmetic defect: it is the contract lying, with nothing checking.
#
# Observed in an Android regression: the manifest declared
#   {"kind": "apk", "entry": "recreated.apk"}
# and its directory contained exactly one object -- the manifest. The job reported Succeeded after
# 2h20m with a 7.8MB trajectory, and produced no score at all.

GRADEABLE = "gradeable"
# The agent ran and produced nothing gradeable. A legitimate zero: it MUST stay in the denominator,
# because "the model failed" is the result, and dropping it would flatter every average.
SCORE_ZERO = "score_zero"
# The agent never ran. Scoring this zero fabricates a data point and silently moves the fixed
# denominator, so it must be reported as a failure instead of graded.
INFRA_FAILURE = "infra_failure"


def _substantial(p: Path) -> bool:
    """Present AND not empty.

    ``.exists()`` is not enough: a 0-byte ``recreated.apk`` satisfies it and would be graded.
    android's own artifact store gate has always used ``exists_nonempty``; this is the on-disk equivalent.
    Directories count as substantial when they hold anything, because a macOS ``.app`` IS a
    directory and has no meaningful size.
    """
    try:
        if p.is_dir():
            return any(p.iterdir())
        return p.is_file() and p.stat().st_size > 0
    except OSError:
        return False


def _contained(root: Path, name: str) -> Path | None:
    """``root/name``, or None if ``name`` escapes ``root``.

    The manifest is read from an artifact directory a recreation agent had write access to, so its
    strings are UNTRUSTED input. An absolute path, a ``..``, or a symlink pointing outside would
    otherwise let a declaration send the boundary check -- and then eval -- at a file of the
    manifest author's choosing.
    """
    # Windows manifests use "\\" as the separator, and this module runs on POSIX too. Translating
    # rather than rejecting is deliberate: a blanket refusal of "\\" would reject a legitimate
    # nested windows declaration like src\\bin\\app.exe, while a traversal written that way
    # (..\\..\\etc) would slip past as one odd filename. After translation the containment check
    # below decides both cases. A filename cannot contain "\\" on Windows, so this loses nothing.
    name = str(name).replace("\\", "/")
    # Redundant with the containment check below on POSIX (root / "/etc" yields "/etc", which then
    # fails containment) -- a mutation test confirmed no case distinguishes them. Kept anyway so an
    # attacker-controlled absolute path is refused BEFORE resolve() touches the filesystem with it.
    if not name or Path(name).is_absolute():
        return None
    cand = root / name
    try:
        resolved = cand.resolve()
        base = root.resolve()
    except OSError:
        return None
    if resolved != base and not resolved.is_relative_to(base):
        return None
    return cand


def read_manifest(dest_dir) -> dict | None:
    """The manifest ACTUALLY on disk, or None when absent/unreadable/not an object."""
    try:
        raw = (Path(dest_dir) / MANIFEST_NAME).read_text()
    except OSError:
        return None
    try:
        got = json.loads(raw)
    except ValueError:
        return None
    return got if isinstance(got, dict) else None


def verify_declared(dest_dir) -> dict:
    """THE boundary check: does what the ON-DISK manifest declares actually exist?

    Distinct from ``verify()`` below, and the distinction was a real defect: ``verify()`` rebuilds a
    declaration from the CALLER's arguments and never opens the file, so it compares expectation
    against reality and cannot see a manifest on disk that drifted from what the caller intended.
    Eval consumes the file, so the file is what must be checked.

    Returns ``{"ok", "missing", "reasons", "manifest", "root"}``. ``reasons`` carries schema and
    containment problems, which are failures of a different kind from a missing file: one means the
    declaration is unusable, the other that the artifact is not there.
    """
    m = read_manifest(dest_dir)
    if m is None:
        return {
            "ok": False,
            "missing": [],
            "reasons": ["no readable %s" % MANIFEST_NAME],
            "manifest": None,
            "root": str(dest_dir),
        }

    reasons: list[str] = []
    if m.get("schema_version") != SCHEMA_VERSION:
        reasons.append(
            "schema_version %r != %r" % (m.get("schema_version"), SCHEMA_VERSION)
        )
    plat = str(m.get("platform") or "")
    if plat not in PLATFORM_KIND:
        reasons.append("unknown platform %r" % plat)
    kind = str(m.get("kind") or "")
    if kind and kind not in KINDS:
        reasons.append("unknown kind %r" % kind)

    base = Path(dest_dir)
    declared_root = str(m.get("root") or ".")
    if declared_root in ("", "."):
        target = base
    else:
        contained = _contained(base, declared_root)
        if contained is None:
            reasons.append("root %r escapes the artifact directory" % declared_root)
            return {
                "ok": False,
                "missing": [],
                "reasons": reasons,
                "manifest": m,
                "root": str(base),
            }
        target = contained

    missing: list[str] = []
    if m.get("archive"):
        # The payload is inside a tarball. Previously this returned ok unconditionally, so a
        # declared archive that was never written would have passed the boundary.
        arch = _contained(base, str(m["archive"]))
        if arch is None:
            reasons.append("archive %r escapes the artifact directory" % m["archive"])
        elif not _substantial(arch):
            missing.append(str(m["archive"]))
        return {
            "ok": not missing and not reasons,
            "missing": missing,
            "reasons": reasons,
            "manifest": m,
            "root": str(target),
        }

    if not target.is_dir():
        missing.append(str(target))
    else:
        for key in ("build", "launch", "entry"):
            name = m.get(key)
            if not name:
                continue
            cand = _contained(target, str(name))
            if cand is None:
                reasons.append("%s %r escapes the artifact directory" % (key, name))
            elif not _substantial(cand):
                missing.append(str(name))

    return {
        "ok": not missing and not reasons,
        "missing": missing,
        "reasons": reasons,
        "manifest": m,
        "root": str(target),
    }


def verify(dest_dir, platform: str, root: str = ".", **kw) -> dict:
    """Does what the CALLER intends to declare exist under ``dest_dir``?

    An expectation check, used by ``write_manifest`` before it writes. It builds the declaration
    from its arguments and does NOT read the file -- so it is the wrong tool at the boundary, where
    the on-disk manifest is what eval consumes. Use ``verify_declared`` there.
    """
    m = manifest(platform, root, **kw)
    target = (
        Path(dest_dir) / m["root"] if m["root"] not in ("", ".") else Path(dest_dir)
    )
    if m.get("archive"):
        return {"ok": True, "missing": [], "checked": [], "root": str(target)}
    if not target.is_dir():
        return {
            "ok": False,
            "missing": [str(target)],
            "checked": [],
            "root": str(target),
        }
    named = [str(m[k]) for k in ("build", "launch", "entry") if m.get(k)]
    missing = [n for n in named if not _substantial(target / n)]
    return {
        "ok": not missing,
        "missing": missing,
        "checked": named,
        "root": str(target),
    }


def grading_verdict(*, artifact_ok: bool, agent_ran: bool) -> str:
    """Whether this run may be graded, and if not, which kind of not.

    THE distinction that keeps the benchmark honest, and the reason this is not simply "no artifact
    => fail the job":

        agent ran,   artifact present  -> GRADEABLE
        agent ran,   artifact missing  -> SCORE_ZERO
                                             (a real result; keep it in the denominator)
        agent absent, either way       -> INFRA_FAILURE   (no result; grading it invents one)

    Both failing rows look identical in a score file -- 0/N -- which is exactly why three runs got
    away with it tonight. The evidence for ``agent_ran`` is deliberately the caller's: only the
    stage knows whether it has a trajectory, a turn count or an elapsed clock.
    """
    if not agent_ran:
        return INFRA_FAILURE
    return GRADEABLE if artifact_ok else SCORE_ZERO


def write_manifest(
    dest_dir, platform: str, root: str, *, provisional: bool = False, **kw
) -> str:
    """Write the manifest into ``dest_dir`` (the task's artifact dir). Returns the path.

    Best-effort by contract: failing to write a pointer must never fail a scored run, because
    the canonical root can still be resolved directly.

    ``provisional=True`` means "the artifact does not exist yet, and that is expected" -- android
    writes its manifest BEFORE the agent runs, so the pointer survives a resume from artifact store
    (stages/pipeline.sh: "Written up front so it is present even when the stage is later resumed").
    Such a write skips the existence check, because warning on every android run would make the
    warning worthless on the run where it matters. Verification for a provisional manifest happens
    at the boundary instead -- ``verify()`` plus ``grading_verdict()`` before eval consumes it.
    """
    d = Path(dest_dir)
    m = manifest(platform, root, **kw)
    if not provisional:
        checked = verify(dest_dir, platform, root, **kw)
        if not checked["ok"]:
            # Warn, do not raise: this function is documented never to fail a scored run, and the
            # canonical root still resolves. Nothing staleness-prone is persisted -- _pointer_holds
            # re-checks on READ, and a flag written now would outlive the condition it describes.
            print(
                "[recreation] WARN: manifest declares files that do not exist under %s: %s"
                % (checked["root"], ", ".join(checked["missing"])),
                file=sys.stderr,
            )
    try:
        d.mkdir(parents=True, exist_ok=True)
        out = d / MANIFEST_NAME
        out.write_text(json.dumps(m, indent=2, ensure_ascii=False))
        return str(out)
    except OSError:
        return ""


def _pointer_holds(holder: Path, target: Path, m: dict) -> bool:
    """Does the manifest's declared root actually contain what the manifest declares?

    Deliberately narrow: only the files the manifest ITSELF names are required. A manifest that
    declares no build/launch (or ships an archive) is accepted whenever its root exists, so this
    cannot reject a shape it does not understand.
    """
    if not target.is_dir():
        return False
    if m.get("archive"):
        archive = _contained(holder, str(m["archive"]))
        return archive is not None and _substantial(archive)
    declared = [m.get(k) for k in ("build", "launch", "entry")]
    named = [str(v) for v in declared if v]
    paths = [_contained(target, name) for name in named]
    return all(path is not None and _substantial(path) for path in paths)


def _same_platform(expected: str, actual: str) -> bool:
    """Linux and Ubuntu are two names for the same current artifact contract."""

    return expected == actual or {expected, actual} <= {"linux", "ubuntu"}


def resolve(task_dir, platform: str, dir_suffix: str = "") -> dict:
    """Locate the recreated app under a local task directory.

    A valid manifest may declare a contained root. Without one, only the canonical path is used;
    historical directory guesses are intentionally unsupported.
    """
    base = Path(task_dir)
    cands = artifact_roots(platform, dir_suffix)
    canonical_targets = {
        target.resolve()
        for candidate in cands
        if (target := _contained(base, candidate)) is not None
    }
    if (platform or "").lower() == "macos":
        canonical_targets.add(base.resolve())

    # A pointer may live at the task root or inside the canonical artifact root. Prefer a pointer
    # whose declared files exist; the lenient pass supports a provisional manifest written before
    # an agent produces its artifact.
    holders = [base]
    for cand in cands:
        holder = _contained(base, cand)
        if holder is not None:
            holders.append(holder)

    pointers = []
    for holder in holders:
        mf = holder / MANIFEST_NAME
        if not mf.is_file():
            continue
        try:
            m = json.loads(mf.read_text())
        except (OSError, ValueError):
            continue  # A corrupt pointer must not hide an artifact at the canonical path.
        if not isinstance(m, dict):
            continue
        actual_platform = str(m.get("platform") or "").lower()
        if m.get("schema_version") != SCHEMA_VERSION or not _same_platform(
            (platform or "").lower(), actual_platform
        ):
            continue
        if m.get("kind") != PLATFORM_KIND.get(actual_platform):
            continue
        root = str(m.get("root") or ".")
        target = holder if root in ("", ".") else _contained(holder, root)
        if target is None or target.resolve() not in canonical_targets:
            continue
        # Validate every path even during the lenient/provisional pass. A missing artifact may be
        # tolerated temporarily; a path that escapes its declared root never is.
        if m.get("archive") and _contained(holder, str(m["archive"])) is None:
            continue
        if any(
            _contained(target, str(m[key])) is None
            for key in ("build", "launch", "entry")
            if m.get(key)
        ):
            continue
        pointers.append((m, holder, target))
    for strict in (True, False):
        for m, holder, target in pointers:
            if strict and not _pointer_holds(holder, target, m):
                continue
            if not strict and not target.is_dir():
                continue
            result = dict(m)
            result["path"] = str(target)
            result["via"] = "manifest"
            return result

    # No valid pointer: accept only the canonical on-disk shape.
    for cand in cands:
        p = _contained(base, cand)
        if p is not None and p.is_dir():
            m = manifest(platform, cand)
            m["path"] = str(p)
            m["via"] = "canonical"
            return m
    return {}


def artifact_manifest_key(prefix: str, task_id: str) -> str:
    """The ONE key a consumer needs to read to locate any platform's recreated app."""
    return "%s/%s/%s" % (prefix.rstrip("/"), task_id, MANIFEST_NAME)


def _build_parser():
    import argparse

    p = argparse.ArgumentParser(
        description="Write / resolve the recreated-app pointer (one definition, five platforms)."
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    w = sub.add_parser("write", help="write recreation_manifest.json into --dest")
    w.add_argument("--dest", required=True, help="the dir the manifest describes")
    w.add_argument("--platform", required=True, choices=sorted(set(PLATFORM_KIND)))
    w.add_argument(
        "--root",
        default=".",
        help="recreated-app root RELATIVE to --dest (default '.', i.e. --dest itself)",
    )
    w.add_argument("--archive", default="", help="tarball member, for macOS")
    w.add_argument(
        "--entry", default="", help="app entry inside the root, when not obvious"
    )
    w.add_argument(
        "--provisional",
        action="store_true",
        help=(
            "the artifact does not exist yet and that is expected (android writes its pointer "
            "before the agent runs). Skips the existence check; verify at the boundary instead."
        ),
    )
    v = sub.add_parser(
        "verify",
        help="check that a declared artifact exists; exit 2 when the declaration does not hold",
    )
    v.add_argument("--dest", required=True)
    v.add_argument("--platform", required=True, choices=sorted(set(PLATFORM_KIND)))
    v.add_argument("--root", default=".")
    v.add_argument("--archive", default="")
    v.add_argument("--entry", default="")
    v.add_argument(
        "--agent-ran",
        choices=("yes", "no", "unknown"),
        default="unknown",
        help=(
            "did the agent actually run? Decides SCORE_ZERO (a real 0, keep it in the "
            "denominator) vs INFRA_FAILURE (no result; grading it invents one). 'unknown' "
            "reports the artifact check only and does not offer a verdict."
        ),
    )
    r = sub.add_parser("resolve", help="locate the recreated app under a task dir")
    r.add_argument("--task-dir", required=True)
    r.add_argument("--platform", required=True, choices=sorted(set(PLATFORM_KIND)))
    r.add_argument("--dir-suffix", default="")
    return p


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    if args.cmd == "write":
        out = write_manifest(
            args.dest,
            args.platform,
            args.root,
            provisional=args.provisional,
            archive=args.archive,
            entry=args.entry,
        )
        print(
            "[recreation] manifest -> %s" % out
            if out
            else "[recreation] WARN: could not write manifest to %s" % args.dest
        )
    elif args.cmd == "verify":
        # The ON-DISK manifest, because that is what eval consumes. --platform/--root/--archive/
        # --entry are accepted for compatibility and ignored here: checking the caller's
        # expectation instead of the file is what made the first version miss drift entirely.
        res = verify_declared(args.dest)
        if args.agent_ran != "unknown":
            res["verdict"] = grading_verdict(
                artifact_ok=res["ok"], agent_ran=args.agent_ran == "yes"
            )
        print(json.dumps(res, ensure_ascii=False, indent=2))
        # exit 2, not 1: a shell caller must be able to tell "the declaration does not hold" from
        # a usage error, because the two demand opposite responses.
        return 0 if res["ok"] else 2
    else:
        got = resolve(args.task_dir, args.platform, args.dir_suffix)
        print(json.dumps(got, ensure_ascii=False, indent=2) if got else "{}")
        return 0 if got else 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
