"""Load and render executable runtime assets shipped with RecreationBench.

Platform adapters may parameterize a script before uploading it, but the script itself
belongs in a normal source file.  Keeping path resolution and placeholder validation here
prevents each adapter from growing another embedded shell/PowerShell implementation.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Mapping

SCRIPTS_ROOT = Path(__file__).resolve().parents[1]
_PLACEHOLDER = re.compile(r"__[A-Z][A-Z0-9_]*__")


def path(relative: str | Path) -> Path:
    """Return a source asset below ``scripts/``, rejecting path traversal."""

    candidate = (SCRIPTS_ROOT / relative).resolve()
    if candidate != SCRIPTS_ROOT and SCRIPTS_ROOT not in candidate.parents:
        raise ValueError(f"runtime asset escapes scripts/: {relative}")
    if not candidate.is_file():
        raise FileNotFoundError(f"runtime asset does not exist: {relative}")
    return candidate


def load_text(relative: str | Path) -> str:
    """Read one UTF-8 runtime asset exactly as it will be uploaded."""

    return path(relative).read_text(encoding="utf-8")


def render_text(relative: str | Path, replacements: Mapping[str, object]) -> str:
    """Render an asset and fail if its explicit ``__NAME__`` contract drifts.

    Replacement keys are literal strings because a few templates intentionally include
    surrounding syntax (for example ``# __VLM_TRANSPORT__``).  Every key must occur and no
    placeholder may remain after rendering.
    """

    rendered = load_text(relative)
    for marker, value in replacements.items():
        if marker not in rendered:
            raise ValueError(f"runtime marker missing from {relative}: {marker}")
        rendered = rendered.replace(marker, str(value))
    unresolved = sorted(set(_PLACEHOLDER.findall(rendered)))
    if unresolved:
        raise ValueError(
            f"unresolved runtime markers in {relative}: {', '.join(unresolved)}"
        )
    return rendered
