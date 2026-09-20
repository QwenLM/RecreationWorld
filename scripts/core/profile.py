"""Profile resolution: one release setting, five thin per-platform overlays.

The released pipeline runs the same benchmark five times over, and most of a profile is
the part that MUST NOT differ between them -- the model, the thinking mode, the agent
budget, the compaction trigger, the VLM judge, the cua-driver pin. Those decide whether
two platforms' scores can be compared at all, and they were copied by hand into ~39
profiles. They had already drifted: macOS left `recreation_timeout` unset and inherited a
2h budget while the others got 6h (an A/B put that at roughly +20pp), and android compacted
at 1000000 against everyone else's 1048576.

So a profile `extends:` a base and states only what is genuinely its own. The scope and the
frozen-suite input live in the base, which is what makes "the released pipeline is
recreation -> eval on the unified suite" a single fact rather than five copies of one.

`extends` is a path relative to the extending file (or absolute), and may be a list applied
left to right. Resolution is depth-first, so a base may itself extend something.

Nested mappings are merged KEY-WISE. Under a flat update an overlay that sets a single
`extra_params` key silently drops every other key the base declared there; the recursive
merge keeps inherited settings intact.
"""

from __future__ import annotations

import os
import pathlib
import re
from typing import Any

_EXTENDS_KEY = "extends"
_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")


def merge(base: Any, overlay: Any) -> Any:
    """Overlay wins, except that two mappings are merged key-wise rather than replaced."""
    if isinstance(base, dict) and isinstance(overlay, dict):
        out = dict(base)
        for k, v in overlay.items():
            out[k] = merge(out[k], v) if k in out else v
        return out
    return overlay


def _load_yaml(path: pathlib.Path) -> dict:
    import yaml

    if not path.is_file():
        raise FileNotFoundError(f"profile not found: {path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError(
            f"profile must be a mapping, got {type(data).__name__}: {path}"
        )
    return data


def _extends_of(data: dict) -> list[str]:
    raw = data.get(_EXTENDS_KEY)
    if raw is None:
        return []
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list) and all(isinstance(x, str) for x in raw):
        return list(raw)
    raise ValueError(
        f"`{_EXTENDS_KEY}` must be a string or a list of strings, got {raw!r}"
    )


def _resolve(path: pathlib.Path, seen: tuple[pathlib.Path, ...]) -> dict:
    real = path.resolve()
    if real in seen:
        chain = " -> ".join(p.name for p in (*seen, real))
        raise ValueError(f"`{_EXTENDS_KEY}` cycle: {chain}")
    data = _load_yaml(path)
    resolved: dict = {}
    for parent in _extends_of(data):
        ppath = pathlib.Path(parent)
        if not ppath.is_absolute():
            ppath = path.parent / ppath
        resolved = merge(resolved, _resolve(ppath, (*seen, real)))
    merged = merge(resolved, data)
    merged.pop(_EXTENDS_KEY, None)
    return merged


def _expand_environment(value: Any, *, source: pathlib.Path) -> Any:
    """Resolve ``${NAME}`` and ``${NAME:-default}`` in a merged profile.

    Infrastructure addresses belong to the deployment environment, not the source tree.
    Required variables fail while loading the profile, before a job can be submitted with a
    literal placeholder. Expansion happens after inheritance so an overlay may replace a base
    value without needing the base value's environment variable.
    """
    if isinstance(value, dict):
        return {
            key: _expand_environment(item, source=source) for key, item in value.items()
        }
    if isinstance(value, list):
        return [_expand_environment(item, source=source) for item in value]
    if not isinstance(value, str) or "${" not in value:
        return value

    def replace(match: re.Match[str]) -> str:
        name, fallback = match.group(1), match.group(2)
        resolved = os.environ.get(name)
        if resolved:
            return resolved
        if fallback is not None:
            return fallback
        raise ValueError(f"profile {source} requires environment variable {name}")

    return _ENV.sub(replace, value)


def load_profile(path: str | pathlib.Path) -> dict:
    """Read a profile YAML and apply its `extends:` chain. Returns the merged mapping."""
    source = pathlib.Path(path)
    return _expand_environment(_resolve(source, ()), source=source)
