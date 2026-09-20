#!/usr/bin/env python3
"""Wire a frozen Android test kit to the shared five-platform VLM judge.

The frozen suite keeps ownership of assertions, navigation, result recording and its fixed
denominator. The model prompt, image encoding, HTTP transport and verdict parsing are runtime
infrastructure and come from ``vlm_judge.py``, exactly as on the other four platforms.
"""

from __future__ import annotations

import argparse
import ast
import re
from pathlib import Path

_BASE_ASSIGNMENT = re.compile(
    r"(?m)^VLM_BASE_URL\s*=\s*(?P<value>[^\n#]+?)(?P<suffix>\s*(?:#.*)?)$"
)
_DEFAULT_KEY_ASSIGNMENT = re.compile(
    r"(?m)^_VLM_DEFAULT_KEY\s*=\s*(?P<value>[^\n#]+?)(?P<suffix>\s*(?:#.*)?)$"
)
_SHIM_MARKER = "# RB_SHARED_VLM_JUDGE_V1"
_SHIM_PATH = Path(__file__).with_name("shared_vlm_shim.py.inc")



def _literal_string(raw: str, name: str) -> str:
    try:
        value = ast.literal_eval(raw.strip())
    except (SyntaxError, ValueError) as exc:
        raise ValueError(f"{name} is not a string literal: {raw.strip()!r}") from exc
    if not isinstance(value, str):
        raise ValueError(f"{name} is not a string literal: {raw.strip()!r}")
    return value


def normalize_source(source: str) -> str:
    """Return a runtime-shared-judge kit or raise on an unknown source shape."""
    if not re.search(r"(?m)^\s*import\s+os(?:\s|$|,)", source):
        raise ValueError("android_testgen_kit.py does not import os")

    base = _BASE_ASSIGNMENT.search(source)
    if not base:
        raise ValueError("android_testgen_kit.py has no VLM_BASE_URL assignment")
    base_expr = base.group("value").strip()
    if "os.environ.get" not in base_expr and "os.getenv" not in base_expr:
        fallback = _literal_string(base_expr, "VLM_BASE_URL")
        replacement = (
            'VLM_BASE_URL = os.environ.get("VLM_BASE_URL", '
            f'{fallback!r}).rstrip("/")' + base.group("suffix")
        )
        source = source[: base.start()] + replacement + source[base.end() :]

    default_key = _DEFAULT_KEY_ASSIGNMENT.search(source)
    if default_key:
        _literal_string(default_key.group("value"), "_VLM_DEFAULT_KEY")
        replacement = '_VLM_DEFAULT_KEY = ""' + default_key.group("suffix")
        source = (
            source[: default_key.start()] + replacement + source[default_key.end() :]
        )

    if _SHIM_MARKER not in source:
        if not re.search(r"(?m)^def\s+call_vlm\s*\(", source):
            raise ValueError(
                "android_testgen_kit.py has no call_vlm compatibility point"
            )
        source = source.rstrip() + "\n\n" + _SHIM_PATH.read_text(encoding="utf-8")

    compile(source, "android_testgen_kit.py", "exec")
    return source


def normalize_file(path: Path) -> bool:
    source = path.read_text(encoding="utf-8")
    normalized = normalize_source(source)
    changed = normalized != source
    if changed:
        tmp = path.with_name(path.name + ".rb-vlm.tmp")
        tmp.write_text(normalized, encoding="utf-8")
        tmp.replace(path)
    return changed


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("kit", type=Path)
    args = parser.parse_args()
    changed = normalize_file(args.kit)
    print(
        "==> frozen VLM judge: "
        + ("wired to shared implementation" if changed else "already shared")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
