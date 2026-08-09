"""Shared filesystem locations for source-checkout deployments."""

from __future__ import annotations

import os
from pathlib import Path


def _project_root() -> Path:
    override = os.environ.get("OSKERNEL_PROJECT_ROOT", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


PROJECT_ROOT = _project_root()
SOURCE_ROOT = PROJECT_ROOT / "src"
