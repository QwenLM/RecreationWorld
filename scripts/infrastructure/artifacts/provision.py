"""Prepare the optional driver selected by the artifact composition boundary."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping

from .factory import (
    ArtifactStoreConfigurationError,
    load_artifact_driver,
    selected_artifact_backend,
)


def ensure_backend_driver(environ: Mapping[str, str] | None = None) -> int:
    values = os.environ if environ is None else environ
    try:
        backend = selected_artifact_backend(values)
        if backend in {"filesystem", "file", "local"}:
            return 0
        driver = load_artifact_driver(backend, values)
    except ArtifactStoreConfigurationError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    provision = getattr(driver, "ensure_artifact_driver", None)
    if not callable(provision):
        return 0
    return int(provision(values))


def main() -> int:
    return ensure_backend_driver()


if __name__ == "__main__":
    raise SystemExit(main())
