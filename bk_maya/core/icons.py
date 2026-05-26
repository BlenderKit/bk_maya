"""Icon registry for the BlenderKit Maya plugin.

Loads ``bk_maya/data/icons/*.png`` and ``*.jpg`` as ``QPixmap`` objects on
first access and caches them for the session.  The same icon set is used
by the Blender addon (copied at setup time via ``maya_module.py``).

Usage::

    from bk_maya.core.icons import icon, icon_path

    pix: QPixmap = icon("blenderkit_logo")   # 'blenderkit_logo.png'
    pix = icon("thumbnail_notready", ext="jpg")
    path: str    = icon_path("free_plan")
"""

from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Icon directory
# ---------------------------------------------------------------------------

_ICON_DIR: str = os.path.join(os.path.dirname(os.path.dirname(__file__)), "data", "icons")
"""Absolute path to the icons directory shipped with bk_maya."""

# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

_cache: dict[str, "QPixmap"] = {}   # name (no ext) → QPixmap


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def icon_path(name: str, ext: str = "png") -> str:
    """Return the absolute filesystem path for icon *name*.

    ``name`` should be the base filename without extension,
    e.g. ``"blenderkit_logo"`` or ``"thumbnail_notready"``.
    """
    return os.path.join(_ICON_DIR, f"{name}.{ext}")


def icon(name: str, ext: str = "png", size: int | None = None) -> "QPixmap":
    """Return a (cached) ``QPixmap`` for icon *name*.

    If the file is missing a placeholder transparent pixmap is returned so
    callers never have to guard against ``None``.

    Parameters
    ----------
    name:
        Base filename without extension.
    ext:
        File extension, default ``"png"``.
    size:
        If given, the pixmap is scaled to *size × size* (keeping aspect ratio).
    """
    # Lazy Qt import — only available inside Maya / after Qt is initialised.
    from qtpy.QtGui import QPixmap
    from qtpy.QtCore import Qt

    cache_key = f"{name}.{ext}"
    pix = _cache.get(cache_key)
    if pix is None:
        path = icon_path(name, ext)
        if os.path.exists(path):
            pix = QPixmap(path)
            if pix.isNull():
                log.warning("Icon loaded as null pixmap: %s", path)
                pix = QPixmap(1, 1)
                pix.fill(Qt.transparent)
        else:
            log.debug("Icon not found: %s", path)
            pix = QPixmap(1, 1)
            pix.fill(Qt.transparent)
        _cache[cache_key] = pix

    if size is not None and not pix.isNull():
        return pix.scaled(size, size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
    return pix


def notready_pixmap(size: int) -> "QPixmap":
    """Return the ``thumbnail_notready.jpg`` scaled to *size × size*."""
    return icon("thumbnail_notready", ext="jpg", size=size)


def not_available_pixmap(size: int) -> "QPixmap":
    """Return the ``thumbnail_not_available.jpg`` scaled to *size × size*."""
    return icon("thumbnail_not_available", ext="jpg", size=size)


def logo_pixmap(size: int = 32) -> "QPixmap":
    """Return the BlenderKit logo scaled to *size × size*."""
    return icon("blenderkit_logo", size=size)
