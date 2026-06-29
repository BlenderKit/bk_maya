"""Blendkit Maya plugin entry point.

This file is discovered by Maya via the MAYA_PLUG_IN_PATH entry written in
blendkit_dev_hl.mod and appears in Maya's Plug-in Manager as maya_plugin.py.

Loading it (manually or via Auto load) will:
1. Ensure bk_maya/lib (qtpy, packaging) and the repo root are on sys.path.
2. Register Blendkit commands and UI panels (populated incrementally).
"""

from __future__ import annotations

import os
import sys

import maya.api.OpenMaya as om2

# Make the addon root (parent of bk_maya/) importable so the centralised
# version module can be read at import time.
_BK_DIR = os.path.dirname(os.path.abspath(__file__))  # .../bk_maya
_ADDON_ROOT = os.path.dirname(_BK_DIR)  # addon root
if _ADDON_ROOT not in sys.path:
    sys.path.insert(0, _ADDON_ROOT)

try:
    from bk_maya._version import get_version as _get_version

    PLUGIN_VERSION = _get_version()
except Exception:  # pragma: no cover - defensive: never block plug-in load
    PLUGIN_VERSION = "0.1.dev"

VENDOR = "Blender Kit s.r.o."


def maya_useNewAPI() -> None:
    """Declare Maya Python API 2.0 usage."""


def _ensure_paths() -> None:
    """Add bk_maya/ and bk_maya/lib to sys.path if not already present.

    The .mod file sets PYTHONPATH at Maya startup, but this is a reliable
    fallback for cases where the module was loaded after startup or the
    PYTHONPATH entries were not yet applied.
    """
    # __file__ is <repo>/bk_maya/maya_plugin.py
    bk_maya_dir = os.path.dirname(os.path.abspath(__file__))
    lib_dir = os.path.join(bk_maya_dir, "lib")
    for extra in (bk_maya_dir, lib_dir):
        if os.path.isdir(extra) and extra not in sys.path:
            sys.path.insert(0, extra)


def _add_shelf_button() -> None:
    """Add a Blendkit button to the active Maya shelf."""
    import maya.cmds as cmds  # type: ignore
    import maya.mel as mel  # type: ignore

    # Ensure there's a shelf to add to
    top_shelf = mel.eval("$tmpVar=$gShelfTopLevel")
    shelves = cmds.tabLayout(top_shelf, query=True, tabLabel=True) or []
    shelf_name = "Blendkit"

    if shelf_name not in shelves:
        mel.eval(f'addNewShelfTab "{shelf_name}";')

    # Avoid duplicate buttons on reload
    existing = cmds.shelfLayout(shelf_name, query=True, childArray=True) or []
    for btn in existing:
        if cmds.shelfButton(btn, query=True, exists=True):
            lbl = cmds.shelfButton(btn, query=True, label=True)
            if lbl == "BKit":
                return

    cmds.shelfButton(
        label="BKit",
        parent=shelf_name,
        annotation="Open Blendkit Asset Bar",
        command="import bk_maya.ui.asset_bar as _ab; _ab.open_asset_bar()",
        sourceType="python",
        image="menuIconFile.png",  # generic icon; replace with bk icon later
    )


def initializePlugin(plugin: om2.MObject) -> None:
    _ensure_paths()
    om2.MFnPlugin(plugin, VENDOR, PLUGIN_VERSION)
    print(f"[Blendkit] Plugin initialized (v{PLUGIN_VERSION})")
    try:
        _add_shelf_button()
    except Exception as exc:
        print(f"[Blendkit] Shelf button not added: {exc}")


def uninitializePlugin(plugin: om2.MObject) -> None:
    print("[Blendkit] Plugin uninitialized.")
