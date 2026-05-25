"""Global state for the BlenderKit Maya plugin.

Mirrors the structure of the Blender addon's ``global_vars.py`` but without
any ``bpy`` dependency so it can run inside or outside Maya.
"""

from __future__ import annotations

import os
from logging import DEBUG, INFO, WARNING
from typing import Any

# ── Logging levels ────────────────────────────────────────────────────────────

LOGGING_LEVEL_BLENDERKIT: int = INFO
"""Log level for all ``bk_maya.*`` loggers."""

LOGGING_LEVEL_IMPORTED: int = WARNING
"""Log level for third-party library loggers (urllib3, requests, …)."""

# Honour the same env-var as the Blender addon so devs have a single switch.
if os.environ.get("BLENDERKIT_DEBUG", "0") == "1":
    LOGGING_LEVEL_BLENDERKIT = DEBUG

# ── Server / API ──────────────────────────────────────────────────────────────

SERVER: str = os.environ.get("BLENDERKIT_SERVER", "https://www.blenderkit.com")
"""Base URL for the BlenderKit API.  Override with BLENDERKIT_SERVER env-var."""

# ── Runtime state ─────────────────────────────────────────────────────────────

DATA: dict[str, Any] = {
    "images available": {},
}
"""Shared runtime dictionary for in-memory caches."""
