"""BlenderKit Maya plugin entry point.

Only this file appears in Maya's Plug-in Manager.
Loading it:
  1. Adds the repo root and bk_maya/lib to sys.path so all bk_maya.* imports work.
  2. Installs a logging handler that routes Python log records to Maya's output window.
  3. Adds a "BlenderKit" top-level menu to the main Maya window.
"""

from __future__ import annotations

import logging
import os
import sys

import maya.api.OpenMaya as om2

PLUGIN_VERSION = "0.1.0"
VENDOR = "BlenderKit s.r.o."
_MENU_NAME = "BlenderKitMenu"


# ---------------------------------------------------------------------------
# Logging — route Python log records to Maya's Script Editor output
# ---------------------------------------------------------------------------


class _MayaLogHandler(logging.Handler):
    """Forwards Python logging to om2.MGlobal so output appears in Script Editor."""

    def emit(self, record: logging.LogRecord) -> None:
        msg = self.format(record)
        try:
            if record.levelno >= logging.ERROR:
                om2.MGlobal.displayError(msg)
            elif record.levelno >= logging.WARNING:
                om2.MGlobal.displayWarning(msg)
            else:
                om2.MGlobal.displayInfo(msg)
        except Exception:
            pass  # never crash Maya's main thread from a log handler


def _install_maya_log_handler() -> None:
    """Add a Maya Script-Editor handler to the bk_maya root logger.

    Must be called *after* _ensure_paths() so bk_maya.core.log is importable.
    configure_loggers() is called first (stdout handler + BlenderKit formatter),
    then this handler is appended so logs go to *both* stdout and Script Editor.
    """
    from bk_maya.core.log import BlenderKitFormatter

    root = logging.getLogger("bk_maya")
    if not any(isinstance(h, _MayaLogHandler) for h in root.handlers):
        handler = _MayaLogHandler()
        handler.setFormatter(
            BlenderKitFormatter(
                fmt="%(levelname)s%(name)s: %(message)s",
                datefmt="%H:%M:%S",
            )
        )
        root.addHandler(handler)
        print("[BlenderKit] Maya log handler installed.")


# ---------------------------------------------------------------------------
# Path bootstrap
# ---------------------------------------------------------------------------


def _ensure_paths(plugin_dir: str) -> None:
    """Add repo root and bk_maya/lib to sys.path.

    *plugin_dir* is returned by MFnPlugin.loadPath() — the directory that
    contains this file, i.e. <repo>/bk_maya/plugins/.
    """
    bk_maya_dir = os.path.dirname(plugin_dir)
    repo_dir = os.path.dirname(bk_maya_dir)
    lib_dir = os.path.join(bk_maya_dir, "lib")
    for extra in (repo_dir, lib_dir):
        if os.path.isdir(extra) and extra not in sys.path:
            sys.path.insert(0, extra)
            print(f"[BlenderKit] sys.path += {extra}")


# ---------------------------------------------------------------------------
# Top-level menu
# ---------------------------------------------------------------------------


def _build_menu() -> None:
    import maya.cmds as cmds  # type: ignore
    import maya.mel as mel  # type: ignore

    # Remove stale menu (safe to re-run on reload)
    if cmds.menu(_MENU_NAME, exists=True):
        cmds.deleteUI(_MENU_NAME, menu=True)
        print("[BlenderKit] Removed stale menu.")

    main_window = mel.eval("$_bk_tmp = $gMainWindow")
    cmds.menu(_MENU_NAME, label="BlenderKit", parent=main_window, tearOff=True)

    # ── Asset bar ────────────────────────────────────────────────────────────
    cmds.menuItem(
        label="Open Asset Bar",
        annotation="Dock the BlenderKit asset browser panel",
        command=("import bk_maya.ui.asset_bar as _ab; _ab.open_asset_bar()"),
    )

    cmds.menuItem(divider=True)

    # ── Account ──────────────────────────────────────────────────────────────
    cmds.menuItem(
        label="Log In…",
        annotation="Authenticate with your BlenderKit account",
        command=(
            "import threading, bk_maya.core.auth as _a;"
            "threading.Thread(target=_a.login, daemon=True).start();"
            "print('[BlenderKit] Browser login started.')"
        ),
    )
    cmds.menuItem(
        label="Log Out",
        annotation="Revoke stored credentials",
        command=("import bk_maya.core.auth as _a; _a.logout();print('[BlenderKit] Logged out.')"),
    )

    cmds.menuItem(divider=True)

    # ── Settings ─────────────────────────────────────────────────────────────
    cmds.menuItem(
        label="Settings…",
        annotation="Open BlenderKit preferences window",
        command=("import bk_maya.ui.settings_dialog as _sd; _sd.open_settings()"),
    )

    cmds.menuItem(divider=True)

    # ── About ─────────────────────────────────────────────────────────────────
    cmds.menuItem(
        label=f"About  (v{PLUGIN_VERSION})",
        command=f"print('[BlenderKit] BlenderKit Maya plugin v{PLUGIN_VERSION} — BlenderKit s.r.o.')",
    )

    print(f"[BlenderKit] Menu '{_MENU_NAME}' added to main window.")


def _remove_menu() -> None:
    import maya.cmds as cmds  # type: ignore

    if cmds.menu(_MENU_NAME, exists=True):
        cmds.deleteUI(_MENU_NAME, menu=True)
        print("[BlenderKit] Menu removed.")


# ---------------------------------------------------------------------------
# Maya plugin API
# ---------------------------------------------------------------------------


def maya_useNewAPI() -> None:
    """Declare Maya Python API 2.0 usage."""


def initializePlugin(plugin: om2.MObject) -> None:
    fn = om2.MFnPlugin(plugin, VENDOR, PLUGIN_VERSION)
    _ensure_paths(fn.loadPath())

    # Configure all bk_maya.* loggers (stdout + API-key masking + emoji prefix)
    from bk_maya.core.log import configure_loggers

    configure_loggers()

    # Also pipe to Maya's Script Editor
    _install_maya_log_handler()

    # Auto-load the placement-locator plug-in (drag-to-place visual).
    # It's a separate Maya plug-in file living next to this one, mirroring
    # the proxor pattern (one .py plug-in per locator/draw-override).
    try:
        import maya.cmds as cmds

        plugin_name = "placement_locator"
        plugin_path = os.path.join(fn.loadPath(), plugin_name + ".py")
        if not cmds.pluginInfo(plugin_name, query=True, loaded=True):
            cmds.loadPlugin(plugin_path, quiet=True)
        # Persist across Maya restarts.
        cmds.pluginInfo(plugin_name, edit=True, autoload=True)
        print(f"[BlenderKit] {plugin_name} plug-in auto-loaded.")
    except Exception as exc:
        print(f"[BlenderKit] Placement locator not auto-loaded: {exc}")

    try:
        _build_menu()
    except Exception as exc:
        print(f"[BlenderKit] Menu not created: {exc}")
    print(f"[BlenderKit] Plugin initialized (v{PLUGIN_VERSION})")


def uninitializePlugin(plugin: om2.MObject) -> None:
    om2.MFnPlugin(plugin)
    try:
        _remove_menu()
    except Exception as exc:
        print(f"[BlenderKit] Menu removal error: {exc}")

    # Stop the local blenderkit-client process we spawned (if any).
    try:
        from bk_maya.core import client_lib

        client_lib.shutdown()
    except Exception as exc:
        print(f"[BlenderKit] Client shutdown error: {exc}")

    # Leave the placement_locator plug-in loaded - it has its own
    # uninitializePlugin which will run when Maya itself unloads it (or
    # when the user does `cmds.unloadPlugin("placement_locator")`).  We do
    # NOT force-unload here because the user may unload BlenderKit while
    # still using the locator.

    print("[BlenderKit] Plugin uninitialized.")
