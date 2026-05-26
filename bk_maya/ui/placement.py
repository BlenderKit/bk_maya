"""BlenderKit Maya – drag-to-place asset system.

Architecture
============
Drag starts from an :class:`AssetTile` (inside the PySide panel) and places
the asset into the Maya 3D viewport.

The visualisation is rendered by a custom locator node + draw override
(``bkPlacementLocator``) registered in :mod:`bk_maya.plugins.placement_locator`.
The draw override reads the live placement snapshot from this module's
:func:`get_drag_snapshot` accessor each frame, so this file just keeps the
state up-to-date and triggers viewport refreshes.

::

    AssetTile.mouseMoveEvent
        └─ start_drag(asset_data, thumb_path)
               └─ DragSession.start()
                      ├─ QApplication.setOverrideCursor(thumb pixmap)
                      ├─ create bkPlacementLocator transform node
                      └─ QApplication.installEventFilter(self)
                             ├─ MouseMove  → ray-cast → update state → refresh VP
                             ├─ Wheel      → rotate_y ± 15°  → refresh VP
                             ├─ LMB up     → _trigger_download() + cleanup
                             ├─ RMB up     → cancel
                             └─ Escape     → cancel

Ray casting
===========
1. Try geometry hit: iterate visible MFnMesh nodes with
   ``MFnMesh.closestIntersection`` (API 2.0).
2. Fallback: intersect the Y=0 floor plane.
"""
from __future__ import annotations

import logging
import math
import os
from dataclasses import asdict, dataclass, field
from typing import Any

log = logging.getLogger("bk_maya.placement")

# ---------------------------------------------------------------------------
# Qt imports (deferred so the module is importable outside Maya)
# ---------------------------------------------------------------------------
try:
    from qtpy.QtCore import QEvent, QObject, QPoint, Qt, QTimer
    from qtpy.QtGui import QColor, QCursor, QPainter, QPen, QPixmap
    from qtpy.QtWidgets import QApplication, QWidget
    try:
        from qtpy.QtCore import QAbstractNativeEventFilter
    except ImportError:  # pragma: no cover
        QAbstractNativeEventFilter = None  # type: ignore
    _QT = True
except ImportError:  # pragma: no cover
    _QT = False
    QAbstractNativeEventFilter = None  # type: ignore

# How many pixels the mouse must travel before drag starts.
DRAG_THRESHOLD = 8

# Rotation step per wheel tick (degrees).
WHEEL_STEP = 15.0


# ---------------------------------------------------------------------------
# Win32 low-level mouse hook
#
# WHY: Maya's 3D viewport is a native HWND with its own window procedure;
# mouse events delivered to it never enter Qt's event dispatch, so neither
# ``QApplication.installEventFilter`` nor ``QAbstractNativeEventFilter`` can
# see wheel rotations or button releases while the cursor is over the view.
# The only mechanism that reliably observes those events is a system-wide
# low-level mouse hook (``SetWindowsHookExW`` with ``WH_MOUSE_LL``), which
# runs in Maya's main thread before the OS dispatches the message to any
# window proc.  We accumulate wheel deltas and button-state into module-
# level slots and let the ~60 Hz cursor poll consume them — that way the
# rotation and the drop are updated from exactly the same code path that
# updates the position.
# ---------------------------------------------------------------------------

import ctypes
from ctypes import wintypes

_WH_MOUSE_LL = 14
_WM_MOUSEWHEEL_LL = 0x020A
_WM_LBUTTONUP_LL = 0x0202
_WM_RBUTTONUP_LL = 0x0205

# Accumulated wheel delta (raw, signed; multiples of 120 per notch).
_wheel_accum: int = 0
# Last-known mouse-button state (mirrors WM_*BUTTONUP transitions).
_lmb_up_pending: bool = False
_rmb_up_pending: bool = False

_hook_handle = None       # HHOOK
_hook_proc_ref = None     # keep CFUNCTYPE alive
_hook_installed = False


class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [
        ("pt", wintypes.POINT),
        ("mouseData", wintypes.DWORD),
        ("flags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


def _ll_mouse_proc(nCode, wParam, lParam):
    """Low-level mouse hook callback.

    Runs on the dedicated hook thread (see ``_install_low_level_hook``).
    Must return quickly — Windows silently uninstalls a WH_MOUSE_LL hook
    whose callback exceeds ``LowLevelHooksTimeout`` (~300 ms).  We only
    bump a couple of module-level counters / flags, which the main
    thread's ``_poll_cursor`` drains on its 16 ms tick.
    """
    global _wheel_accum, _lmb_up_pending, _rmb_up_pending
    try:
        if nCode == 0:  # HC_ACTION
            msg = wParam
            if msg == _WM_MOUSEWHEEL_LL:
                info = ctypes.cast(
                    lParam, ctypes.POINTER(_MSLLHOOKSTRUCT)
                )[0]
                raw = (info.mouseData >> 16) & 0xFFFF
                if raw >= 0x8000:
                    raw -= 0x10000
                # Accumulate; consumed by _poll_cursor.
                _wheel_accum += int(raw)
            elif msg == _WM_LBUTTONUP_LL:
                _lmb_up_pending = True
            elif msg == _WM_RBUTTONUP_LL:
                _rmb_up_pending = True
    except Exception:
        # Never propagate exceptions out of a Win32 hook callback.
        pass
    # Always call next hook; we never consume (Maya still needs to see all events).
    try:
        return ctypes.windll.user32.CallNextHookEx(0, nCode, wParam, lParam)
    except Exception:
        return 0


def _install_low_level_hook() -> None:
    """Install the WH_MOUSE_LL hook on a dedicated thread.

    WHY a dedicated thread:
        Windows requires WH_MOUSE_LL callbacks to return within
        ``LowLevelHooksTimeout`` (~300 ms by default, set in the registry).
        If they don't, the OS *silently removes the hook* — no error, no
        callback to tell us.  Maya's main thread is frequently blocked
        for hundreds of ms while drawing the viewport or running raycasts,
        so a hook installed on the main thread is unreliable: the first
        time Maya does heavy work, the hook is removed and we never see
        another wheel event until something else re-installs it.

        Running the hook on its own thread with its own ``GetMessage``
        loop keeps the callback responsive regardless of what Maya's
        main thread is doing.  The callback only mutates a few
        module-level ints/bools (thread-safe enough under the GIL),
        and ``_poll_cursor`` drains them on the main thread.
    """
    global _hook_installed
    if _hook_installed:
        return
    import threading

    def _hook_thread_main():
        global _hook_handle, _hook_proc_ref, _hook_installed
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            HOOKPROC = ctypes.WINFUNCTYPE(
                ctypes.c_long, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM
            )
            _hook_proc_ref = HOOKPROC(_ll_mouse_proc)
            user32.SetWindowsHookExW.restype = wintypes.HHOOK
            user32.SetWindowsHookExW.argtypes = [
                ctypes.c_int, HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD,
            ]
            user32.GetMessageW.argtypes = [
                ctypes.c_void_p, wintypes.HWND, wintypes.UINT, wintypes.UINT,
            ]
            user32.GetMessageW.restype = ctypes.c_int

            hmod = kernel32.GetModuleHandleW(None)
            _hook_handle = user32.SetWindowsHookExW(
                _WH_MOUSE_LL, _hook_proc_ref, hmod, 0
            )
            if not _hook_handle:
                err = ctypes.get_last_error()
                log.warning(
                    "SetWindowsHookExW failed on hook thread, GetLastError=%d",
                    err,
                )
                _hook_proc_ref = None
                return
            _hook_installed = True
            log.info(
                "Low-level mouse hook installed on dedicated thread "
                "(hhook=%s, tid=%s).",
                _hook_handle, threading.get_ident(),
            )

            # Pump messages until the thread exits (process shutdown).
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception as exc:
            log.warning("Hook thread crashed: %s", exc)

    t = threading.Thread(
        target=_hook_thread_main,
        name="bk_maya.placement.WH_MOUSE_LL",
        daemon=True,
    )
    t.start()



def _drain_wheel_accum() -> int:
    """Return and reset the accumulated wheel delta."""
    global _wheel_accum
    v = _wheel_accum
    _wheel_accum = 0
    return v


def _consume_lmb_up() -> bool:
    """Return True if a WM_LBUTTONUP was observed since last call."""
    global _lmb_up_pending
    if _lmb_up_pending:
        _lmb_up_pending = False
        return True
    return False


def _consume_rmb_up() -> bool:
    global _rmb_up_pending
    if _rmb_up_pending:
        _rmb_up_pending = False
        return True
    return False



def _meters_to_internal() -> float:
    """Return the scale factor that converts 1 meter to Maya's internal unit.

    Maya's *internal* linear unit is always centimeters regardless of the UI
    unit setting, so this always returns 100.0 in practice — but we go through
    ``MDistance`` to remain correct if Autodesk ever changes that.
    """
    try:
        import maya.api.OpenMaya as om2
        return om2.MDistance(1.0, om2.MDistance.kMeters).asUnits(
            om2.MDistance.internalUnit()
        )
    except Exception:
        return 100.0


# ═════════════════════════════════════════════════════════════════════════════
# Placement state  (module-level, queried by the draw override)
# ═════════════════════════════════════════════════════════════════════════════

@dataclass
class _State:
    asset_data: dict[str, Any] = field(default_factory=dict)
    thumb_path: str  = ""
    # bbox in object-local space (matches BlenderKit API bbox_min/bbox_max)
    bbox_min:   tuple[float, float, float] = (-0.5, 0.0, -0.5)
    bbox_max:   tuple[float, float, float] = ( 0.5, 1.0,  0.5)
    # current world placement
    location:   tuple[float, float, float] = (0.0, 0.0, 0.0)
    rotation_y: float = 0.0   # degrees, controlled by mouse wheel
    # hit status
    has_hit:    bool  = False
    hit_floor:  bool  = False
    active:     bool  = False
    # proxor line data (may be empty list)
    proxor_lines: list[list[tuple[float, float, float]]] = field(default_factory=list)


# The active state — read by the draw override every frame.
_active_state: _State = _State()


def get_drag_snapshot() -> dict[str, Any] | None:
    """Return a snapshot dict of the current drag state.

    Called by :class:`bk_maya.plugins.placement_locator.BkPlacementDrawOverride`
    during ``prepareForDraw``.  Returns ``None`` when no drag is in progress.
    """
    if not _active_state.active:
        return None
    return asdict(_active_state)


# ═════════════════════════════════════════════════════════════════════════════
# Maya viewport helpers
# ═════════════════════════════════════════════════════════════════════════════

def _get_viewport_widget() -> "QWidget | None":
    """Return the QWidget for Maya's active 3D viewport.

    Tries four strategies; logs at debug which one won.
    """
    try:
        import maya.OpenMayaUI as omui1
        view = omui1.M3dView.active3dView()
        ptr = view.widget()

        if isinstance(ptr, QWidget):
            return ptr

        if ptr is not None and int(ptr) != 0:
            for sh_name in ("shiboken6", "shiboken2"):
                try:
                    sh = __import__(sh_name)
                    return sh.wrapInstance(int(ptr), QWidget)
                except Exception:
                    pass
    except Exception as e:
        log.debug("BK viewport: M3dView.widget() error: %s", e)

    try:
        import maya.cmds as cmds
        import maya.OpenMayaUI as omui1

        focused = cmds.getPanel(withFocus=True) or ""
        if focused and cmds.getPanel(typeOf=focused) == "modelPanel":
            panels = [focused]
        else:
            panels = cmds.getPanel(type="modelPanel") or []

        for panel in panels:
            try:
                editor = cmds.modelPanel(panel, query=True, modelEditor=True)
                ptr = omui1.MQtUtil.findControl(editor)
                if ptr and int(ptr) != 0:
                    for sh_name in ("shiboken6", "shiboken2"):
                        try:
                            sh = __import__(sh_name)
                            return sh.wrapInstance(int(ptr), QWidget)
                        except Exception:
                            pass
            except Exception:
                pass
    except Exception:
        pass

    best, best_area = None, 0
    for w in QApplication.allWidgets():
        if not w.isVisible():
            continue
        cn = w.metaObject().className() if w.metaObject() else ""
        if any(kw in cn for kw in (
            "GLWidget", "MayaGL", "THoverQ", "modelEditor", "MayaViewport", "ViewportUI",
        )):
            area = w.width() * w.height()
            if area > best_area:
                best, best_area = w, area
    return best


def _raycast_scene(vp_x: int, vp_y: int) -> "tuple[bool, tuple, bool]":
    """Cast a ray from viewport pixel (vp_x, vp_y) (Qt coords).

    Returns ``(has_hit, (x,y,z), hit_floor)``.  ``hit_floor`` is True when the
    Y=0 fallback plane was used because no scene geometry was struck.
    """
    try:
        import maya.api.OpenMaya as om2
        import maya.api.OpenMayaUI as omui2

        view = omui2.M3dView.active3dView()
        vp_h = view.portHeight()
        maya_y = vp_h - vp_y   # Qt top-down → Maya bottom-up

        near_pt = om2.MPoint()
        far_pt  = om2.MPoint()
        view.viewToWorld(int(vp_x), int(maya_y), near_pt, far_pt)

        ray_src = om2.MFloatPoint(near_pt.x, near_pt.y, near_pt.z)
        ray_dir = om2.MFloatVector(
            far_pt.x - near_pt.x,
            far_pt.y - near_pt.y,
            far_pt.z - near_pt.z,
        )
        ray_dir.normalize()

        closest_dist = float("inf")
        best_hit: tuple | None = None

        it = om2.MItDag(om2.MItDag.kDepthFirst, om2.MFn.kMesh)
        while not it.isDone():
            try:
                dag_path = it.getPath()
                if not dag_path.isVisible():
                    it.next()
                    continue

                fn = om2.MFnMesh(dag_path)
                result = fn.closestIntersection(
                    ray_src, ray_dir,
                    om2.MSpace.kWorld,
                    9999999.0,   # maxParam
                    False,       # testBothDirections
                )
                if result is not None:
                    hit_pt = result[0]
                    dx = hit_pt.x - near_pt.x
                    dy = hit_pt.y - near_pt.y
                    dz = hit_pt.z - near_pt.z
                    d = math.sqrt(dx*dx + dy*dy + dz*dz)
                    if 0.001 < d < closest_dist:
                        closest_dist = d
                        best_hit = (float(hit_pt.x), float(hit_pt.y), float(hit_pt.z))
            except Exception:
                pass
            it.next()

        if best_hit:
            return True, best_hit, False

        # Floor plane fallback
        ox, oy, oz = near_pt.x, near_pt.y, near_pt.z
        dx = far_pt.x - near_pt.x
        dy = far_pt.y - near_pt.y
        dz = far_pt.z - near_pt.z
        if abs(dy) > 1e-9:
            t = -oy / dy
            if t > 0:
                # Floor counts as a HIT so the bbox snaps to it (cyan in the
                # draw override).  Returning has_hit=False would leave the
                # bbox at world origin which is never what the user wants.
                return True, (ox + t*dx, 0.0, oz + t*dz), True

        # No hit and the ray is parallel to the floor — project the camera
        # eye-line forward by an arbitrary distance so the bbox is still
        # visible at the cursor depth instead of snapping back to origin.
        t = 1000.0  # 10 m in Maya cm units; comfortably within view
        return False, (ox + t*dx, oy + t*dy, oz + t*dz), False

    except Exception as exc:
        log.debug("Raycast error: %s", exc)

    return False, (0.0, 0.0, 0.0), False


def _refresh_viewport(*, light: bool = False) -> None:
    """Force a redraw of the active viewport so the locator re-evaluates.

    ``light=True`` asks Maya for an asynchronous repaint that only updates
    the currently active 3D view; this is used on the rotation-only hot
    path so wheel scrolls feel instantaneous.  The default
    (``light=False``) does a full ``cmds.refresh(force=True)`` which is
    needed after position changes / raycasts.
    """
    if light:
        try:
            import maya.api.OpenMayaUI as omui2
            omui2.M3dView.active3dView().refresh(False, False)
            return
        except Exception:
            pass  # fall through to the heavier path

    try:
        import maya.cmds as cmds
        cmds.refresh(currentView=True, force=True)
    except Exception:
        try:
            import maya.api.OpenMayaUI as omui2
            omui2.M3dView.active3dView().refresh(force=True)
        except Exception as e:
            log.debug("viewport refresh failed: %s", e)


# ═════════════════════════════════════════════════════════════════════════════
# Locator node lifecycle (transient – created on drag start, deleted on end)
# ═════════════════════════════════════════════════════════════════════════════

_LOCATOR_NAME = "bkPlacementDragLocator"


def _create_locator() -> str | None:
    """Create the placement locator node and return its name.

    Returns ``None`` if the node type isn't registered yet (plugin not loaded).
    """
    try:
        import maya.cmds as cmds

        # Verify the plug-in is loaded and the node type is registered.
        node_types = set(cmds.allNodeTypes() or [])
        if "bkPlacementLocator" not in node_types:
            # Try loading the sibling plug-in by absolute path (the bk_maya
            # core dir is sibling to ui/, and plugins/ is sibling to that).
            here = os.path.dirname(os.path.abspath(__file__))            # …/bk_maya/ui
            bk_maya_dir = os.path.dirname(here)                          # …/bk_maya
            plugin_path = os.path.join(bk_maya_dir, "plugins", "placement_locator.py")
            log.warning(
                "bkPlacementLocator not registered – attempting to load %s",
                plugin_path,
            )
            try:
                if os.path.isfile(plugin_path):
                    cmds.loadPlugin(plugin_path, quiet=True)
                else:
                    cmds.loadPlugin("placement_locator", quiet=True)
            except Exception as exc:
                log.error("loadPlugin(placement_locator) failed: %s", exc)
            node_types = set(cmds.allNodeTypes() or [])
            if "bkPlacementLocator" not in node_types:
                log.error(
                    "bkPlacementLocator node type STILL not registered after "
                    "loadPlugin. Open Windows > Settings/Preferences > Plug-in "
                    "Manager, find 'placement_locator.py', and tick Loaded + "
                    "Auto load."
                )
                return None
            log.info("bkPlacementLocator plug-in loaded on demand.")

        if cmds.objExists(_LOCATOR_NAME):
            cmds.delete(_LOCATOR_NAME)
        node = cmds.createNode(
            "bkPlacementLocator",
            name=_LOCATOR_NAME,
            skipSelect=True,
        )
        # cmds.createNode on a shape-derived type returns the SHAPE name and
        # auto-creates a transform parent.  All our custom attributes live on
        # the shape, so we always target it explicitly.
        try:
            shapes = cmds.listRelatives(node, shapes=True, fullPath=False) or []
            if shapes:
                shape_name = shapes[0]
            else:
                # Already the shape (createNode returned the shape directly).
                shape_name = node
        except Exception:
            shape_name = node
        try:
            cmds.setAttr(shape_name + ".hiddenInOutliner", True)
        except Exception:
            pass
        log.info("Placement locator created: transform=%s shape=%s", node, shape_name)
        return shape_name
    except Exception as exc:
        log.error("Could not create %s: %s", _LOCATOR_NAME, exc)
        return None


def _delete_locator() -> None:
    try:
        import maya.cmds as cmds
        if cmds.objExists(_LOCATOR_NAME):
            cmds.delete(_LOCATOR_NAME)
    except Exception as exc:
        log.debug("Could not delete %s: %s", _LOCATOR_NAME, exc)


# ═════════════════════════════════════════════════════════════════════════════
# Proxor loader (optional – falls back to bbox draw when absent)
# ═════════════════════════════════════════════════════════════════════════════

def _load_proxor_lines(asset_data: dict[str, Any]) -> list[list[tuple]]:
    """Try to load the proxor wireframe for *asset_data* from local cache."""
    asset_base_id = asset_data.get("assetBaseId", "")
    if not asset_base_id:
        return []

    candidates = []
    try:
        from bk_maya.core import global_vars as gv  # type: ignore
        for attr in ("CACHE_DIR", "PROXOR_DIR", "BLENDERKIT_DATA_DIR"):
            p = getattr(gv, attr, None)
            if p:
                candidates.append(os.path.join(str(p), "proxors", f"{asset_base_id}.prxc"))
    except Exception:
        pass
    candidates.append(
        os.path.expanduser(f"~/blenderkit_data/proxors/{asset_base_id}.prxc")
    )

    prxc_path = ""
    for c in candidates:
        if os.path.isfile(c):
            prxc_path = c
            break
    if not prxc_path:
        return []

    try:
        from bl_proxor import prx_format as pf  # type: ignore
        payload = pf.read_prx(prxc_path)
        data = payload.get("data", {})
        positions = data.get("line", {}).get("pos", [])

        # Proxor stores positions in meters (Blender), and Z is up. Convert to Maya's cm units and swap Y/Z.
        scale = _meters_to_internal()
        lines = []
        for i in range(0, len(positions) - 1, 2):
            ax, ay, az = (float(v) for v in positions[i][:3])
            bx, by, bz = (float(v) for v in positions[i + 1][:3])
            a = (ax * scale, az * scale, ay * scale)
            b = (bx * scale, bz * scale, by * scale)
            lines.append([a, b])
        log.debug("Proxor loaded for %s: %d segments", asset_base_id, len(lines))
        return lines
    except Exception as exc:
        log.debug("Proxor load failed for %s: %s", asset_base_id, exc)
        return []


# ═════════════════════════════════════════════════════════════════════════════
# Thumbnail cursor
# ═════════════════════════════════════════════════════════════════════════════

_CURSOR_SIZE = 64


def _make_cursor(thumb_path: str) -> "QCursor":
    pix = QPixmap(thumb_path) if thumb_path and os.path.isfile(thumb_path) else QPixmap()
    if pix.isNull():
        pix = QPixmap(_CURSOR_SIZE, _CURSOR_SIZE)
        pix.fill(QColor(0, 0, 0, 0))
        p = QPainter(pix)
        p.setPen(QPen(QColor(0, 220, 80), 2))
        mid = _CURSOR_SIZE // 2
        p.drawLine(mid, 0, mid, _CURSOR_SIZE)
        p.drawLine(0, mid, _CURSOR_SIZE, mid)
        p.end()
    else:
        pix = pix.scaled(
            _CURSOR_SIZE, _CURSOR_SIZE,
            Qt.KeepAspectRatio, Qt.SmoothTransformation,
        )
    return QCursor(pix, 0, 0)


# ═════════════════════════════════════════════════════════════════════════════
# Drag session (singleton Qt event filter)
# ═════════════════════════════════════════════════════════════════════════════

class DragSession(QObject):
    """Qt event filter driving a single drag-to-place operation."""

    _instance: "DragSession | None" = None

    @classmethod
    def get(cls) -> "DragSession":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        super().__init__()
        self._vp_widget: "QWidget | None" = None
        self._locator_name: str | None = None
        self._download_started = False
        self._poll_timer: "QTimer | None" = None  # polls cursor position

    # ── Public API ────────────────────────────────────────────────────────

    def start(self, asset_data: dict[str, Any], thumb_path: str, delay_locator: bool = False) -> None:
        global _active_state
        if _active_state.active:
            log.debug("DragSession already active; ignoring start()")
            return
        self._delay_locator = delay_locator
        self._drop_fired = False
        # BlenderKit API returns bbox in meters; Maya internal unit is cm.
        scale = _meters_to_internal()

        def _coerce_bbox(v, default):
            if v is None:
                return default
            # Some API endpoints return dicts {"x":..,"y":..,"z":..}; handle both.
            if isinstance(v, dict):
                seq = (v.get("x", 0.0), v.get("y", 0.0), v.get("z", 0.0))
            else:
                try:
                    seq = tuple(v)
                except TypeError:
                    return default
            try:
                return (float(seq[0]) * scale,
                        float(seq[1]) * scale,
                        float(seq[2]) * scale)
            except (TypeError, ValueError, IndexError):
                return default

        default_min = (-0.5 * scale, 0.0,        -0.5 * scale)
        default_max = ( 0.5 * scale, 1.0 * scale, 0.5 * scale)

        # The BlenderKit search endpoint returns the bbox split across six
        # scalar params under ``dictParameters`` — ``boundBoxMinX/Y/Z`` and
        # ``boundBoxMaxX/Y/Z`` (all in meters, Blender Z-up).  The Blender
        # add-on folds those into top-level ``bbox_min`` / ``bbox_max``
        # tuples during search-result parsing (see addon's search.py); the
        # Maya port doesn't (yet), so do it here so dragging any real asset
        # gets its real bounding box instead of the 1 m³ default cube.
        raw_min = asset_data.get("bbox_min")
        raw_max = asset_data.get("bbox_max")
        if raw_min is None or raw_max is None:
            params = (asset_data.get("dictParameters")
                      or asset_data.get("parameters")
                      or {})
            try:
                mn = (float(params["boundBoxMinX"]),
                      float(params["boundBoxMinY"]),
                      float(params["boundBoxMinZ"]))
                mx = (float(params["boundBoxMaxX"]),
                      float(params["boundBoxMaxY"]),
                      float(params["boundBoxMaxZ"]))
                if raw_min is None:
                    raw_min = mn
                if raw_max is None:
                    raw_max = mx
                log.info("bbox extracted from dictParameters: min=%s max=%s",
                         mn, mx)
            except (KeyError, TypeError, ValueError):
                log.warning("Asset %s has no usable bbox – using 1 m default cube",
                            asset_data.get("name", "?"))

        bbox_min = _coerce_bbox(raw_min, default_min)
        bbox_max = _coerce_bbox(raw_max, default_max)

        # Maya is Y-up; BlenderKit/Blender is Z-up. Swap Y/Z so the asset's
        # "up" axis points up in Maya, then re-order min/max per-axis.
        sw_min = (bbox_min[0], bbox_min[2], bbox_min[1])
        sw_max = (bbox_max[0], bbox_max[2], bbox_max[1])
        bbox_min = tuple(min(sw_min[i], sw_max[i]) for i in range(3))
        bbox_max = tuple(max(sw_min[i], sw_max[i]) for i in range(3))

        _active_state = _State(
            asset_data   = asset_data,
            thumb_path   = thumb_path,
            bbox_min     = bbox_min,
            bbox_max     = bbox_max,
            location     = (0.0, 0.0, 0.0),
            rotation_y   = 0.0,
            active       = True,
            proxor_lines = _load_proxor_lines(asset_data),
        )
        log.info(
            "Drag start: asset=%s bbox_min=%s bbox_max=%s",
            asset_data.get("name", "?"), bbox_min, bbox_max,
        )

        # Spawn the locator (draw override starts ticking)
        self._locator_name = None if delay_locator else _create_locator()
        if self._locator_name is None:
            log.warning(
                "bkPlacementLocator node type not registered – "
                "load the BlenderKit plugin via Plug-in Manager."
            )
        else:
            self._publish_bbox_to_locator()
            self._publish_proxor_to_locator()

        # Override cursor with thumbnail
        QApplication.setOverrideCursor(_make_cursor(thumb_path))

        # Find viewport for mouse mapping
        self._vp_widget = _get_viewport_widget()
        if self._vp_widget is None:
            log.warning("BK drag: no Maya 3D viewport widget found")

        # Release any mouse grab held by the asset-bar widget so the
        # app-wide event filter sees every move/release event no matter
        # which widget the cursor crosses (Qt would otherwise route all
        # events to the original press target until LMB release).
        try:
            grabber = QWidget.mouseGrabber()  # type: ignore[attr-defined]
            if grabber is not None:
                grabber.releaseMouse()
        except Exception:
            pass

        QApplication.instance().installEventFilter(self)

        # Also install directly on the viewport widget. Maya's 3D view is a
        # native QOpenGLWidget that often consumes wheel events for camera
        # dolly before the app-level filter can swallow them. A direct widget
        # filter receives the event first and lets us intercept it.
        self._filtered_widgets: list = []
        try:
            if self._vp_widget is not None:
                self._vp_widget.installEventFilter(self)
                self._filtered_widgets.append(self._vp_widget)
                # Walk up the ancestor chain – Maya's modelPanel nests the
                # QOpenGLWidget under several QWidgets and the wheel event
                # may be redirected to one of them.
                w = self._vp_widget.parent()
                depth = 0
                while w is not None and depth < 8:
                    try:
                        w.installEventFilter(self)
                        self._filtered_widgets.append(w)
                    except Exception:
                        pass
                    w = w.parent() if hasattr(w, "parent") else None
                    depth += 1
                # Also walk down to direct children (Maya's view contains a
                # native window surface child that often receives wheel).
                try:
                    for ch in self._vp_widget.findChildren(QWidget):
                        ch.installEventFilter(self)
                        self._filtered_widgets.append(ch)
                except Exception:
                    pass
        except Exception as exc:
            log.warning("Could not install viewport event filter: %s", exc)

        # Install the system-wide low-level mouse hook (once per Maya process).
        # Maya's 3D viewport HWND consumes WM_MOUSEWHEEL / WM_LBUTTONUP before
        # Qt sees them; only a WH_MOUSE_LL hook reliably observes those events.
        # The hook just accumulates state into module-level slots — the active
        # DragSession consumes them from ``_poll_cursor`` so rotation/drop are
        # updated on the same tick as the cursor position.
        _install_low_level_hook()

        # Drive raycasts from a polling timer instead of relying on Qt mouse-
        # move events.  Qt's implicit grab on the asset tile (LMB pressed in
        # the panel and held while dragging into the viewport) routes move
        # events back to the tile, which can mask them from our filter.  A
        # ~60 Hz cursor poll keeps the bbox glued to the cursor regardless.
        try:
            if self._poll_timer is None:
                self._poll_timer = QTimer()
                self._poll_timer.setInterval(16)   # ms (~60 fps)
                # PreciseTimer keeps the cursor poll firing at the requested
                # rate even when Maya's main thread is busy redrawing the
                # viewport. The default CoarseTimer is allowed to be delayed
                # by 5 %+, which can stretch to hundreds of ms under load and
                # makes wheel/click response feel laggy.
                try:
                    self._poll_timer.setTimerType(Qt.PreciseTimer)
                except Exception:
                    pass
                self._poll_timer.timeout.connect(self._poll_cursor)
            self._tick_count = 0
            self._poll_timer.start()
            log.info("Drag poll timer started (interval=%d ms)", self._poll_timer.interval())
        except Exception as exc:
            log.warning("Could not start drag poll timer: %s", exc)

        # In-viewport controls hint (Maya HUD message, no GL text overlay).
        try:
            import maya.cmds as cmds
            cmds.inViewMessage(
                amg=(
                    f"<b>{asset_data.get('name', 'Asset')}</b>  "
                    "&nbsp;|&nbsp;  Wheel: rotate  "
                    "&nbsp;|&nbsp;  LMB: place  "
                    "&nbsp;|&nbsp;  RMB / ESC: cancel"
                ),
                pos="botCenter",
                fade=False,
            )
        except Exception:
            pass

        log.debug(
            "DragSession started: asset=%s proxor=%d locator=%s",
            asset_data.get("name", "?"),
            len(_active_state.proxor_lines),
            self._locator_name,
        )
        _refresh_viewport()

    def cancel(self) -> None:
        log.debug("DragSession cancelled")
        self._cleanup()

    # ── QObject.eventFilter ───────────────────────────────────────────────

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # type: ignore[override]
        if not _active_state.active:
            return False

        et = event.type()

        if et == QEvent.MouseMove:
            self._on_mouse_move(event)
            return False

        if et == QEvent.MouseButtonRelease:
            btn = event.button()
            if btn == Qt.LeftButton:
                self._on_drop()
                return True
            if btn == Qt.RightButton:
                self.cancel()
                return True

        if et == QEvent.KeyPress and event.key() == Qt.Key_Escape:
            self.cancel()
            return True

        if et == QEvent.Wheel:
            return self._on_wheel(event)

        return False

    # ── Private ───────────────────────────────────────────────────────────

    def _global_pos(self, event) -> QPoint:
        """Qt5/Qt6 compatible global mouse position."""
        try:
            return event.globalPos()
        except AttributeError:
            return event.globalPosition().toPoint()

    def _on_mouse_move(self, event: QEvent) -> None:
        # Kept for backwards compat; the heavy lifting is done by
        # ``_poll_cursor`` now, but if the event filter does happen to see
        # a move we let it refresh the state too.
        self._poll_cursor()

    def _poll_cursor(self) -> None:
        if not _active_state.active:
            return
        self._tick_count = getattr(self, "_tick_count", 0) + 1

        # ── 1. Consume mouse-button events from the LL hook ───────────────
        # (RMB before LMB so right-click in mid-drag wins.)
        if _consume_rmb_up():
            log.info("[POLL RMB-UP] cancelling drag")
            self.cancel()
            return
        if _consume_lmb_up():
            log.info("[POLL LMB-UP] firing drop")
            self._on_drop()
            return

        # ── 2. Consume wheel notches from the LL hook ─────────────────────
        # _wheel_accum is updated on the hook thread; one drain per tick.
        # 1 notch = 120 raw units.
        wheel = _drain_wheel_accum()
        rotation_changed = False
        if wheel != 0:
            _active_state.rotation_y += (wheel / 120.0) * WHEEL_STEP
            rotation_changed = True
            log.info("[POLL WHEEL] raw=%d rot_y=%.1f",
                     wheel, _active_state.rotation_y)

        # ── 3. Cursor position → raycast ──────────────────────────────────
        vp = self._vp_widget or _get_viewport_widget()
        if vp is None:
            return
        if vp is not self._vp_widget:
            self._vp_widget = vp

        try:
            gp = QCursor.pos()
        except Exception:
            log.exception("Could not get global cursor position; stopping drag")
            return
        local = vp.mapFromGlobal(gp)
        inside = vp.rect().contains(local)

        # Create the locator on first viewport entry when delay_locator was set.
        if (getattr(self, "_delay_locator", False)
                and self._locator_name is None
                and inside):
            self._locator_name = _create_locator()
            if self._locator_name is not None:
                self._publish_bbox_to_locator()
            self._delay_locator = False
            log.info("Locator created on viewport entry.")

        # No active locator yet → nothing to push.
        if self._locator_name is None:
            # Still need to flush a rotation-only refresh if we accumulated
            # wheel notches before the locator existed.
            return

        # Skip the raycast entirely when the cursor pixel hasn't moved
        # since the last tick.  Otherwise we'd iterate the whole scene DAG
        # every 16 ms even while the user is just spinning the wheel,
        # which starves the polling timer and makes rotation feel laggy.
        position_changed = False
        last_px = getattr(self, "_last_cursor_px", None)
        cur_px = (local.x(), local.y(), inside)
        if inside and cur_px != last_px:
            has_hit, loc, on_floor = _raycast_scene(local.x(), local.y())
            if (loc != _active_state.location
                    or has_hit != _active_state.has_hit
                    or on_floor != _active_state.hit_floor):
                _active_state.location  = loc
                _active_state.has_hit   = has_hit
                _active_state.hit_floor = on_floor
                position_changed = True
        self._last_cursor_px = cur_px

        # ── 4. Push only what changed to the locator node ─────────────────
        if not (position_changed or rotation_changed):
            return

        try:
            import maya.cmds as cmds
            if position_changed:
                loc = _active_state.location
                cmds.setAttr(self._locator_name + ".location",
                             loc[0], loc[1], loc[2], type="double3")
                cmds.setAttr(self._locator_name + ".hasHit",
                             bool(_active_state.has_hit))
                cmds.setAttr(self._locator_name + ".hitFloor",
                             bool(_active_state.hit_floor))
            if rotation_changed:
                cmds.setAttr(self._locator_name + ".rotationY",
                             _active_state.rotation_y)
        except Exception as exc:
            if self._tick_count <= 3:
                log.warning("Failed to push locator state: %s", exc)

        # ── 5. One async, non-blocking refresh per tick ───────────────────
        # M3dView.refresh(all=False, force=False) schedules a repaint of
        # the active view on Maya's next idle, instead of synchronously
        # repainting (which is what cmds.refresh(force=True) does and what
        # was previously starving the polling timer).
        _refresh_viewport(light=True)

    def _publish_bbox_to_locator(self) -> None:
        """Write the cached bbox_min/bbox_max onto the locator shape."""
        if self._locator_name is None:
            return
        try:
            import maya.cmds as cmds
            bmn = _active_state.bbox_min
            bmx = _active_state.bbox_max
            cmds.setAttr(self._locator_name + ".bboxMin", bmn[0], bmn[1], bmn[2], type="double3")
            cmds.setAttr(self._locator_name + ".bboxMax", bmx[0], bmx[1], bmx[2], type="double3")
            log.info("[BBOX] published min=%s max=%s on %s", bmn, bmx, self._locator_name)
        except Exception as exc:
            log.warning("Failed to publish bbox to locator: %s", exc)

    def _publish_proxor_to_locator(self) -> None:
        """Register cached proxor polylines for the locator's draw override."""
        if self._locator_name is None:
            return
        try:
            from bk_maya.plugins import placement_locator as plc
            plc.set_proxor_lines(self._locator_name,
                                 list(_active_state.proxor_lines or []))
            # Seed the label with the asset name; status fills in once
            # the download controller starts firing progress events.
            asset_name = ""
            try:
                asset_name = str(_active_state.asset_data.get("name") or "")
            except Exception:
                pass
            plc.set_label(self._locator_name, name=asset_name, status="Ready to drop")
            log.info("[PROXOR] published %d polyline(s) on %s",
                     len(_active_state.proxor_lines or []), self._locator_name)
        except Exception as exc:
            log.debug("Could not publish proxor lines: %s", exc)

    def _on_wheel(self, event: QEvent) -> bool:
        # We accept wheel events anywhere while a drag session is active
        # (Maya may dispatch the event to a child of the 3D view rather
        # than the QOpenGLWidget itself).  Bounds-check via the global
        # cursor position instead of mapping the event point, which can
        # be in surface-local coordinates of the wrong widget.
        try:
            delta = event.angleDelta().y()
        except AttributeError:
            try:
                delta = event.delta()
            except Exception:
                delta = 0
        if delta == 0:
            return False

        vp = self._vp_widget
        try:
            gp = QCursor.pos()
        except Exception:
            gp = None
        if vp is not None and gp is not None:
            if not vp.rect().contains(vp.mapFromGlobal(gp)):
                return False

        step = WHEEL_STEP if delta > 0 else -WHEEL_STEP
        _active_state.rotation_y += step
        log.warning("[WHEEL] delta=%s new rot_y=%.1f", delta, _active_state.rotation_y)
        if self._locator_name is not None:
            try:
                import maya.cmds as cmds
                cmds.setAttr(self._locator_name + ".rotationY", _active_state.rotation_y)
            except Exception as exc:
                log.warning("Failed to set rotationY on locator: %s", exc)
        _refresh_viewport()
        return True

    # ── Win32 native-filter dispatch ──────────────────────────────────────

    # (Removed — superseded by the WH_MOUSE_LL low-level hook, which
    # feeds _wheel_accum / _lmb_up_pending / _rmb_up_pending consumed
    # directly inside _poll_cursor on the same tick as the cursor position.)

    def _on_drop(self) -> None:
        # Guard against double-fire (native filter + Qt event filter may both arrive).
        if getattr(self, "_drop_fired", False):
            return
        self._drop_fired = True
        # Set download state to 'downloading' (1)
        if self._locator_name is not None:
            try:
                import maya.cmds as cmds
                cmds.setAttr(self._locator_name + ".downloadState", 1)  # downloading
                self._download_started = True
                log.warning("[DROP] setAttr locator=%s downloadState=1 (downloading)", self._locator_name)
            except Exception as exc:
                log.warning("Failed to set download state: %s", exc)
        if _active_state.has_hit:
            self._trigger_download()
        self._cleanup()

    def _trigger_download(self) -> None:
        loc   = _active_state.location
        rot_y = _active_state.rotation_y
        asset = _active_state.asset_data
        log.info(
            "BK drop: '%s' at (%.3f, %.3f, %.3f) rot_y=%.1f°",
            asset.get("name", "?"),
            loc[0], loc[1], loc[2],
            rot_y,
        )
        # Try the real download module if it exists; otherwise log a stub.
        try:
            import importlib
            bk_dl = importlib.import_module("bk_maya.core.download")
            # inverse rotation
            bk_dl.download_asset(
                asset,
                location=loc,
                rotation_y=-rot_y,
                locator_name=self._locator_name or "",
            )
        except ModuleNotFoundError:
            log.info(
                "download module not implemented yet – stub only. "
                "Asset '%s' would be placed at %s rot_y=%.1f°",
                asset.get("name", "?"), loc, rot_y,
            )
        except Exception as exc:
            log.error("Download dispatch failed: %s", exc)

    def _cleanup(self) -> None:
        global _active_state
        _active_state.active = False

        try:
            if self._poll_timer is not None:
                self._poll_timer.stop()
        except Exception:
            pass

        try:
            QApplication.instance().removeEventFilter(self)
        except Exception:
            pass
        # Remove per-widget filters installed during start()
        try:
            for w in getattr(self, "_filtered_widgets", []) or []:
                try:
                    w.removeEventFilter(self)
                except Exception:
                    pass
            self._filtered_widgets = []
        except Exception:
            pass
        try:
            QApplication.restoreOverrideCursor()
        except Exception:
            pass

        # If a download was triggered, hand ownership of the locator off to
        # the download controller — it will delete it on success / failure.
        if self._locator_name is not None and not self._download_started:
            _delete_locator()
            self._locator_name = None
        else:
            self._locator_name = None

        # Clear the persistent HUD hint.
        try:
            import maya.cmds as cmds
            cmds.inViewMessage(clear="botCenter")
        except Exception:
            pass

        self._vp_widget = None
        _refresh_viewport()
        log.debug("DragSession cleaned up")


# ═════════════════════════════════════════════════════════════════════════════
# Public entry point
# ═════════════════════════════════════════════════════════════════════════════

def start_drag(asset_data: dict[str, Any], thumb_path: str) -> None:
    """Entry point for drag from asset bar. Starts drag session, but delays locator creation until mouse enters viewport."""
    DragSession.get().start(asset_data, thumb_path, delay_locator=True)

def place_at_origin(asset_data: dict[str, Any], thumb_path: str) -> None:
    """Place asset at (0,0,0) immediately (no drag)."""
    # Set up state
    global _active_state
    scale = _meters_to_internal()
    def _coerce_bbox(v, default):
        if v is None:
            return default
        if isinstance(v, dict):
            seq = (v.get("x", 0.0), v.get("y", 0.0), v.get("z", 0.0))
        else:
            try:
                seq = tuple(v)
            except TypeError:
                return default
        try:
            return (float(seq[0]) * scale, float(seq[1]) * scale, float(seq[2]) * scale)
        except (TypeError, ValueError, IndexError):
            return default
    default_min = (-0.5 * scale, 0.0,        -0.5 * scale)
    default_max = ( 0.5 * scale, 1.0 * scale, 0.5 * scale)
    bbox_min = _coerce_bbox(asset_data.get("bbox_min"), default_min)
    bbox_max = _coerce_bbox(asset_data.get("bbox_max"), default_max)
    sw_min = (bbox_min[0], bbox_min[2], bbox_min[1])
    sw_max = (bbox_max[0], bbox_max[2], bbox_max[1])
    bbox_min = tuple(min(sw_min[i], sw_max[i]) for i in range(3))
    bbox_max = tuple(max(sw_min[i], sw_max[i]) for i in range(3))
    _active_state = _State(
        asset_data   = asset_data,
        thumb_path   = thumb_path,
        bbox_min     = bbox_min,
        bbox_max     = bbox_max,
        location     = (0.0, 0.0, 0.0),
        rotation_y   = 0.0,
        active       = True,
        proxor_lines = _load_proxor_lines(asset_data),
    )
    log.info("Place at origin: asset=%s bbox_min=%s bbox_max=%s", asset_data.get("name", "?"), bbox_min, bbox_max)
    # Create locator, trigger download immediately
    locator = _create_locator()
    DragSession.get()._trigger_download()
    DragSession.get()._cleanup()
