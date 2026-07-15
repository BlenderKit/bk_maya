r"""Blendkit Maya preferences — load, save, and access plugin settings.

Preferences are stored as JSON at:
  Windows : %USERPROFILE%\\Documents\\maya\\blendkit_prefs.json
  macOS   : ~/Library/Preferences/Autodesk/maya/blendkit_prefs.json
  Linux   : ~/maya/blendkit_prefs.json

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
    return os.path.join(docs, "maya", "blendkit_prefs.json")


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
    """Thumbnail tile size in the asset bar (pixels, 48-256)."""

    # ── Updates ────────────────────────────────────────────────────────────
    check_for_updates: bool = True
    """Check GitHub for a newer plugin release on startup."""

    include_alpha_updates: bool = False
    """Also consider rolling alpha (pre-release) builds when checking for updates."""

    # ── Files ──────────────────────────────────────────────────────────────
    global_dir: str = ""
    """Root directory for downloaded assets.  Empty = platform default."""

    max_resolution: str = "2048"
    """Cap texture sizes on import.  One of: 512 1024 2048 4096 8192 ORIGINAL."""

    blender_exe: str = ""
    """Path to blender executable.  Empty = auto-detect (Blender 5.0+ required)."""

    blender_version_cache: dict[str, str] = dataclasses.field(default_factory=dict)
    """Cache of detected Blender versions, mapping ``"<exe>|<mtime>|<size>"`` to
    a ``"X.Y.Z"`` string.  Lets us skip re-running ``blender --version`` on every
    drag once a path has been validated (see :func:`blender_runner.query_blender_version`)."""

    import_method: str = "stage"
    """How models are brought into the scene on drop.  One of:
    ``import``    — merge the USD geometry into the current scene;
    ``reference`` — link the USD as a Maya file reference (editable, unloadable);
    ``stage``     — load the USD as a native Maya USD stage (mayaUsdProxyShape, default)."""

    # ── Search filters ─────────────────────────────────────────────────────
    search_texture_resolution: bool = False
    """Limit search results by texture resolution."""

    search_texture_resolution_min: int = 256
    """Minimum texture resolution filter (px)."""

    search_texture_resolution_max: int = 4096
    """Maximum texture resolution filter (px)."""

    search_free_only: bool = False
    """Return only free assets."""

    search_my_assets_only: bool = False
    """Return only assets uploaded by the logged-in user."""

    search_bookmarked_only: bool = False
    """Return only assets the logged-in user has bookmarked."""

    search_quality_limit: int = 0
    """Minimum quality rating (0 = no limit, 1-5 scale)."""

    search_license: str = "ANY"
    """License filter (ANY, FREE, ROYALTY_FREE, FULL, USAGE_RIGHTS)."""

    search_animated_only: bool = False
    """Return only animated assets."""

    search_poly_count: bool = False
    """Limit search results by polygon count."""

    search_poly_count_min: int = 0
    """Minimum polygon count (vertices)."""

    search_poly_count_max: int = 0
    """Maximum polygon count (vertices)."""

    search_style: str = "ANY"
    """Model style filter (REALISTIC, PAINTERLY, LOWPOLY, ANIME, 2D_VECTOR, 3D_GRAPHICS, OTHER, ANY)."""

    search_condition: str = "UNSPECIFIED"
    """Model condition filter (UNSPECIFIED, NEW, USED, OLD, DESOLATE)."""

    search_design_year: bool = False
    """Limit search results by design year."""

    search_design_year_min: int = 1950
    """Minimum design year."""

    search_design_year_max: int = 2030
    """Maximum design year."""

    search_file_size: bool = False
    """Limit search results by file size."""

    search_file_size_min: int = 0
    """Minimum file size (MB)."""

    search_file_size_max: int = 500
    """Maximum file size (MB)."""

    search_geometry_nodes: bool = False
    """Show only assets using geometry nodes."""

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
        """Return global_dir, falling back to a sensible default.

        Matches the Blender addon's ``default_global_dict()``: uses
        ``$XDG_DATA_HOME/blenderkit_data`` if set (typically Linux),
        otherwise ``~/blenderkit_data``.
        """
        if self.global_dir:
            return self.global_dir
        home = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~")
        return os.path.join(home, "blenderkit_data")

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {f.name: getattr(self, f.name) for f in dataclasses.fields(self) if not f.name.startswith("_")}

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
