"""Helpers for loading dotenv files without leaking parser warnings to users."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


def load_dotenv_quiet(dotenv_path: str | Path | None = None, **kwargs: Any) -> bool:
    """Load dotenv values while suppressing python-dotenv parse warnings."""

    logger = logging.getLogger("dotenv.main")
    previous_disabled = logger.disabled
    previous_level = logger.level
    logger.disabled = True
    try:
        return load_dotenv(dotenv_path, **kwargs)
    finally:
        logger.disabled = previous_disabled
        logger.setLevel(previous_level)
