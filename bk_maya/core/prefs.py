"""BlenderKit Maya preferences — load, save, and access plugin settings.

Preferences are stored as JSON at:
  Windows : %USERPROFILE%\\Documents\\maya\\blenderkit_prefs.json
  macOS   : ~/Library/Preferences/Autodesk/maya/blenderkit_prefs.json
  Linux   : ~/maya/blenderkit_prefs.json

Usage:
    from bk_maya.core.prefs import prefs   # singleton

    prefs.thumbnail_size = 192
    prefs.save()
"""

from __future__ import annotations

import ctypes
import dataclasses
import json
import logging
import os
import sys
from typing import Any

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Storage path
# ---------------------------------------------------------------------------

def _prefs_path() -> str:
    if sys.platform == "win32":
        buf = ctypes.create_unicode_buffer(260)
        ctypes.windll.shell32.SHGetFolderPathW(None, 0x0005, None, 0, buf)
        docs = buf.value or os.path.join(os.environ.get("USERPROFILE", ""), "Documents")
    elif sys.platform == "darwin":
        docs = os.path.expanduser("~/Library/Preferences/Autodesk/maya")
    else:
        docs = os.path.expanduser("~/maya")
    return os.path.join(docs, "maya", "blenderkit_prefs.json")


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------

@dataclasses.dataclass
class _Prefs:
    # ── General ────────────────────────────────────────────────────────────
    show_on_start: bool = False
    """Open asset bar automatically when Maya starts."""

    tips_on_start: bool = True
    """Show usage tips on Maya startup."""

    thumbnail_size: int = 128
    """Thumbnail tile size in the asset bar (pixels, 48–256)."""

    # ── Files ──────────────────────────────────────────────────────────────
    global_dir: str = ""
    """Root directory for downloaded assets.  Empty = platform default."""

    max_resolution: str = "2048"
    """Cap texture sizes on import.  One of: 512 1024 2048 4096 8192 ORIGINAL."""

    # ── Search filters ─────────────────────────────────────────────────────
    search_texture_resolution: bool = False
    """Limit search results by texture resolution."""

    search_texture_resolution_min: int = 256
    """Minimum texture resolution filter (px)."""

    search_texture_resolution_max: int = 4096
    """Maximum texture resolution filter (px)."""

    search_free_only: bool = False
    """Return only free assets."""

    # ── Networking ─────────────────────────────────────────────────────────
    proxy_which: str = "SYSTEM"
    """Proxy mode: SYSTEM | ENVIRONMENT | NONE | CUSTOM."""

    proxy_address: str = ""
    """Custom proxy URL (used when proxy_which == CUSTOM)."""

    ssl_verification: bool = True
    """Verify SSL certificates on outgoing requests."""

    # ── Internal ───────────────────────────────────────────────────────────
    _path: str = dataclasses.field(default="", init=False, repr=False, compare=False)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def global_dir_resolved(self) -> str:
        """Return global_dir, falling back to a sensible default."""
        if self.global_dir:
            return self.global_dir
        if sys.platform == "win32":
            base = os.path.join(os.environ.get("USERPROFILE", ""), "Documents")
        elif sys.platform == "darwin":
            base = os.path.expanduser("~/Documents")
        else:
            base = os.path.expanduser("~")
        return os.path.join(base, "BlenderKit")

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            f.name: getattr(self, f.name)
            for f in dataclasses.fields(self)
            if not f.name.startswith("_")
        }

    def update_from_dict(self, data: dict[str, Any]) -> None:
        valid = {f.name for f in dataclasses.fields(self) if not f.name.startswith("_")}
        for key, value in data.items():
            if key in valid:
                try:
                    setattr(self, key, value)
                except Exception as exc:
                    log.warning("Ignoring bad pref %r=%r: %s", key, value, exc)

    def save(self) -> None:
        path = _prefs_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with open(path, "w", encoding="utf-8") as fh:
                json.dump(self.to_dict(), fh, indent=2)
            log.debug("Prefs saved to %s", path)
        except OSError as exc:
            log.error("Could not save prefs: %s", exc)

    def load(self) -> None:
        path = _prefs_path()
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
            self.update_from_dict(data)
            log.debug("Prefs loaded from %s", path)
        except FileNotFoundError:
            log.debug("No prefs file yet at %s — using defaults.", path)
        except Exception as exc:
            log.warning("Could not load prefs (%s) — using defaults.", exc)


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

prefs = _Prefs()
prefs.load()
