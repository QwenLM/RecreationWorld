"""Select an artifact transport at the process composition boundary.

The benchmark ships a filesystem store. Other transports are optional plugins
selected by the explicit ``RB_ARTIFACT_DRIVER`` module name, so a public source
tree can omit every deployment integration.
"""

from __future__ import annotations

import importlib
import os
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from types import ModuleType
from typing import Any

from core.artifact_store import ArtifactRetryPolicy, ArtifactStore

from .filesystem import FilesystemArtifactStore


class ArtifactStoreConfigurationError(ValueError):
    """Raised when the selected artifact backend is not fully configured."""


_LOCAL_BACKENDS = {"filesystem", "file", "local"}
_MODULE_NAME = re.compile(r"^[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*$")


def selected_artifact_backend(environ: Mapping[str, str] | None = None) -> str:
    """Return the normalized backend selected by the portable environment contract."""
    values = os.environ if environ is None else environ
    backend = str(values.get("RB_ARTIFACT_BACKEND", "")).strip().lower()
    if backend:
        return backend
    if str(values.get("RB_ARTIFACT_ROOT", "")).strip():
        return "filesystem"
    raise ArtifactStoreConfigurationError(
        "RB_ARTIFACT_BACKEND is required when RB_ARTIFACT_ROOT is not set"
    )


def artifact_driver_module_name(
    backend: str, environ: Mapping[str, str] | None = None
) -> str:
    """Resolve an optional backend without encoding a cloud provider in core."""
    values = os.environ if environ is None else environ
    module_name = str(values.get("RB_ARTIFACT_DRIVER", "")).strip()
    if not module_name:
        raise ArtifactStoreConfigurationError(
            "RB_ARTIFACT_DRIVER is required for a non-filesystem artifact backend"
        )
    if not _MODULE_NAME.fullmatch(module_name):
        raise ArtifactStoreConfigurationError(
            f"invalid RB_ARTIFACT_DRIVER module name: {module_name!r}"
        )
    return module_name


def load_artifact_driver(
    backend: str, environ: Mapping[str, str] | None = None
) -> ModuleType:
    """Import the selected optional backend and report a configuration error clearly."""
    module_name = artifact_driver_module_name(backend, environ)
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        missing_name = str(exc.name or "")
        if not (
            missing_name == module_name
            or (missing_name and module_name.startswith(missing_name + "."))
        ):
            raise
        raise ArtifactStoreConfigurationError(
            f"artifact backend {backend!r} is unavailable; install or include "
            f"driver module {module_name!r}"
        ) from exc
    return module


def validate_artifact_environment(environ: Mapping[str, str] | None = None) -> str:
    """Validate backend configuration without importing a provider SDK."""
    values = os.environ if environ is None else environ
    backend = selected_artifact_backend(values)
    if backend in _LOCAL_BACKENDS:
        if not str(values.get("RB_ARTIFACT_ROOT", "")).strip():
            raise ArtifactStoreConfigurationError(
                "RB_ARTIFACT_ROOT is required for the filesystem artifact backend"
            )
        return "filesystem"

    driver = load_artifact_driver(backend, values)
    validator = getattr(driver, "validate_artifact_environment", None)
    if not callable(validator):
        raise ArtifactStoreConfigurationError(
            f"artifact driver {driver.__name__!r} does not define "
            "validate_artifact_environment"
        )
    try:
        validator(values)
    except ArtifactStoreConfigurationError:
        raise
    except ValueError as exc:
        raise ArtifactStoreConfigurationError(str(exc)) from exc
    return backend


def artifact_environment_for_remote(
    environ: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Select portable artifact variables that a remote runtime must inherit."""
    values = os.environ if environ is None else environ
    selected_artifact_backend(values)
    return {
        name: str(value)
        for name, value in sorted(values.items())
        if name.startswith("RB_ARTIFACT_") and str(value)
    }


def artifact_store_from_environment(
    environ: Mapping[str, str] | None = None,
    *,
    retry_policy: ArtifactRetryPolicy | None = None,
    log: Callable[[str], None] = print,
    backend_options: Mapping[str, Any] | None = None,
) -> ArtifactStore:
    """Create the configured store while keeping providers outside core code."""
    values = os.environ if environ is None else environ
    backend = validate_artifact_environment(values)
    if backend == "filesystem":
        return FilesystemArtifactStore(Path(str(values["RB_ARTIFACT_ROOT"])))

    driver = load_artifact_driver(backend, values)
    create = getattr(driver, "artifact_store_from_environment", None)
    if not callable(create):
        raise ArtifactStoreConfigurationError(
            f"artifact driver {driver.__name__!r} does not define "
            "artifact_store_from_environment"
        )
    return create(
        values,
        retry_policy=retry_policy,
        log=log,
        backend_options=dict(backend_options or {}),
    )
