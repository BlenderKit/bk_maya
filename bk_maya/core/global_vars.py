"""Global state for the Blendkit Maya plugin.

Mirrors the structure of the Blender addon's ``global_vars.py`` but without
any ``bpy`` dependency so it can run inside or outside Maya.
"""

from __future__ import annotations

import os
from logging import DEBUG, INFO, WARNING
from typing import Any

# ── Logging levels ────────────────────────────────────────────────────────────

LOGGING_LEVEL_BLENDKIT: int = INFO
"""Log level for all ``bk_maya.*`` loggers."""

LOGGING_LEVEL_IMPORTED: int = WARNING
"""Log level for third-party library loggers (urllib3, requests, …)."""

# Honour the same env-var as the Blender addon so devs have a single switch.
if os.environ.get("BLENDKIT_DEBUG", "0") == "1":
    LOGGING_LEVEL_BLENDKIT = DEBUG

# ── Server / API ──────────────────────────────────────────────────────────────

SERVER: str = os.environ.get("BLENDKIT_SERVER", "https://www.blendkit.com")
"""Base URL for the Blendkit API.  Override with BLENDKIT_SERVER env-var."""

# ── Client ────────────────────────────────────────────────────────────────────

CLIENT_VERSION: str = "v1.11"
"""Minor-series pin (``vX.Y``) of the Blendkit Go client this add-on targets.

bk_client auto-bumps the PATCH version on every merge, so we pin only the minor
series and let the build resolve the exact ``vX.Y.Z`` (the newest patch of the
series), baking it into ``client/RESOLVED_VERSION``. The client's HTTP API path
(``/vX.Y``) is derived from this minor pin — non-breaking client changes bump
the patch only, breaking changes bump the minor/major."""

# ── Runtime state ─────────────────────────────────────────────────────────────

DATA: dict[str, Any] = {
    "images available": {},
}
"""Shared runtime dictionary for in-memory caches."""
