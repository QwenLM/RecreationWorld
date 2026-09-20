"""The unified RB instance descriptor: one 8-key schema for all five platforms.

Differences between platforms are expressed as ``null``, not as different shapes,
so a loader never branches on platform to parse the file. What each platform must
fill is enforced by :data:`INVARIANTS` instead of by a discriminator field
(``kind`` was dropped because it is fully determined by ``platform`` today).

    {"schema_version", "instance_id", "platform", "repo", "commit",
     "patches", "package", "license"}

Honesty rule (the reason this validator exists): ``null`` means "not applicable to
this platform" and is legal, but an empty string or a placeholder means "should have
been filled and wasn't" and is a hard error. Real releases shipped
``model="xxx"``, ``baseline_model=""`` and ``baseline_package=""`` — shapes that pass
any key-presence check while carrying no information.
"""

from __future__ import annotations

import re
from pathlib import PurePosixPath

KEYS = (
    "schema_version",
    "instance_id",
    "platform",
    "repo",
    "commit",
    "patches",
    "package",
    "license",
)

PLATFORMS = ("ubuntu", "macos", "windows", "android", "web")

# platform -> (fields that must be non-null, fields that must be null)
INVARIANTS = {
    "ubuntu": (("repo", "commit"), ("package",)),
    "macos": (("repo", "commit"), ("package",)),
    "windows": (("repo", "commit"), ("package",)),
    "android": (("repo", "commit", "package"), ()),
    "web": ((), ("repo", "commit", "package")),
}

# Values that are shaped like data but carry none. ``noassertion`` is GitHub's
# licensee output when it cannot classify a license — recording it would discard the
# fact that the license IS knowable by reading the repo's LICENSE file by hand.
PLACEHOLDERS = {
    "",
    "xxx",
    "todo",
    "tbd",
    "none",
    "null",
    "unknown",
    "n/a",
    "-",
    "noassertion",
}

PATCH_REASONS = (
    "suppress_nondeterminism",
    "disable_network",
    "disable_update_check",
    "disable_telemetry",
    "build_fix",
)

_SPDX_OPS = {"AND", "OR", "WITH"}
_SPDX_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+-]*$")
_LICENSE_REF = re.compile(r"^LicenseRef-[A-Za-z0-9.-]+$")


def _spdx_problem(expr: str) -> str | None:
    """Structural check for an SPDX license expression.

    Validates SHAPE, not membership in the SPDX License List (that would need the
    list itself). It exists to reject the prose that real compliance records carry —
    e.g. ``"U.S. Federal Government work — Public Domain (17 U.S.C. § 105)"``, which
    is a copyright *status*, not a license. Use ``LicenseRef-US-Gov-PublicDomain``
    for licenses that are not on the SPDX list, and keep attribution/ShareAlike
    prose in the compliance record instead.
    """
    tokens = expr.replace("(", " ").replace(")", " ").split()
    if not tokens:
        return "license is empty"
    prev_was_license = False
    for t in tokens:
        if t.upper() in _SPDX_OPS:
            if not prev_was_license:
                return f"operator {t!r} must follow a license id"
            prev_was_license = False
            continue
        if t.lower().startswith("licenseref"):
            # a bare "LicenseRef-" also matches _SPDX_TOKEN, so check it explicitly
            if not _LICENSE_REF.match(t):
                return f"{t!r} is not a valid LicenseRef-<id>"
        elif not _SPDX_TOKEN.match(t):
            return f"{t!r} is not an SPDX id (prose belongs in the compliance record)"
        if prev_was_license:
            # two ids in a row = prose such as "U.S. Federal" or "CC BY 4.0"
            return f"{t!r} follows an id with no AND/OR — looks like prose, not SPDX"
        prev_was_license = True
    if not prev_was_license:
        return "expression ends with an operator"
    return None


def _is_placeholder(v) -> bool:
    return isinstance(v, str) and v.strip().lower() in PLACEHOLDERS


def validate_instance(inst: dict) -> list[str]:
    """Return a list of problems; empty list means the descriptor is valid."""
    errs: list[str] = []

    extra = set(inst) - set(KEYS)
    missing = set(KEYS) - set(inst)
    if extra:
        errs.append(f"unexpected keys: {sorted(extra)}")
    if missing:
        errs.append(f"missing keys: {sorted(missing)}")
    if missing:
        return errs  # further checks would be noise

    if inst["schema_version"] != 1:
        errs.append(f"schema_version must be 1, got {inst['schema_version']!r}")

    platform = inst["platform"]
    if platform not in PLATFORMS:
        errs.append(f"platform must be one of {PLATFORMS}, got {platform!r}")
        return errs

    iid = inst["instance_id"]
    prefix = f"{platform}/"
    if not isinstance(iid, str) or not iid.startswith(prefix) or len(iid) <= len(prefix):
        errs.append(f"instance_id must be '{platform}/<app>', got {iid!r}")

    # placeholders are never acceptable, in any field
    for k in ("instance_id", "repo", "commit", "package", "license"):
        if _is_placeholder(inst[k]):
            errs.append(f"{k} is a placeholder ({inst[k]!r}) — use null or a real value")

    required, forbidden = INVARIANTS[platform]
    for k in required:
        if inst[k] is None:
            errs.append(f"{platform} requires {k} to be non-null")
    for k in forbidden:
        if inst[k] is not None:
            errs.append(f"{platform} requires {k} to be null, got {inst[k]!r}")

    if inst["commit"] is not None:
        c = inst["commit"]
        if not (isinstance(c, str) and len(c) == 40 and all(ch in "0123456789abcdef" for ch in c)):
            errs.append(f"commit must be a full 40-hex sha, got {c!r}")

    if inst["repo"] is not None and not str(inst["repo"]).startswith(("http://", "https://")):
        errs.append(f"repo must be a URL, got {inst['repo']!r}")

    if inst["license"] is not None:
        problem = _spdx_problem(str(inst["license"]))
        if problem:
            errs.append(f"license: {problem}")

    # android package must look like an applicationId (this is what silently broke:
    # the kit's fallback yielded "" because argv[1] was the CLI subcommand name)
    if inst["package"] is not None:
        pkg = str(inst["package"])
        if "." not in pkg:
            errs.append(f"package must contain a '.', got {pkg!r}")
        # The field means the REFERENCE app's applicationId. Recreations are installed
        # side-by-side under a '.clone' suffix, so that value here is the wrong app.
        if pkg.endswith(".clone"):
            errs.append(f"package {pkg!r} is a recreation applicationId; record the reference's")

    errs.extend(_validate_patches(inst, platform))
    return errs


def _validate_patches(inst: dict, platform: str) -> list[str]:
    errs: list[str] = []
    patches = inst["patches"]
    if not isinstance(patches, list):
        return [f"patches must be a list, got {type(patches).__name__}"]
    if platform == "web" and patches:
        return ["web is a frozen mirror; patches must be []"]
    for i, p in enumerate(patches):
        if not isinstance(p, dict):
            errs.append(f"patches[{i}] must be an object")
            continue
        for f in ("path", "reason", "note", "sha256"):
            if not p.get(f):
                errs.append(f"patches[{i}].{f} is required")
        if p.get("reason") and p["reason"] not in PATCH_REASONS:
            errs.append(f"patches[{i}].reason {p['reason']!r} not in {PATCH_REASONS}")
        if p.get("path"):
            path = PurePosixPath(str(p["path"]))
            if (
                path.is_absolute()
                or ".." in path.parts
                or path.parts[:2] != ("reference", "patches")
            ):
                errs.append(f"patches[{i}].path must live under reference/patches/")
        sha256 = p.get("sha256")
        if sha256 and not re.fullmatch(r"[0-9a-f]{64}", str(sha256)):
            errs.append(f"patches[{i}].sha256 must be 64 lowercase hex characters")
    return errs
