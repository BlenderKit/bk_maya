"""Blendkit Maya - drag-to-place asset system.

Architecture
============
Drag starts from an :class:`AssetTile` (inside the PySide panel) and places
the asset into the Maya 3D viewport.

The visualization is rendered by a custom locator node + draw override
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

# The WH_MOUSE_LL hook below is a Windows-only workaround. On macOS / Linux
# the Qt event filter already sees every wheel / button event, so the hook
# must never start there (``ctypes.WinDLL`` doesn't exist off Windows and
# would crash the hook thread).
_IS_WINDOWS = hasattr(ctypes, "WinDLL")

_WH_MOUSE_LL = 14
_WM_MOUSEWHEEL_LL = 0x020A
_WM_LBUTTONUP_LL = 0x0202
_WM_RBUTTONUP_LL = 0x0205

# Accumulated wheel delta (raw, signed; multiples of 120 per notch).
_wheel_accum: int = 0
# Last-known mouse-button state (mirrors WM_*BUTTONUP transitions).
_lmb_up_pending: bool = False
_rmb_up_pending: bool = False

_hook_handle = None  # HHOOK
_hook_proc_ref = None  # keep CFUNCTYPE alive
_hook_installed = False

# ── Raycast acceleration cache ────────────────────────────────────────────
# ``_raycast_scene`` runs every ~16 ms cursor tick during a drag and would
# otherwise iterate *every* mesh in the scene, calling ``closestIntersection``
# with no acceleration structure.  As placed models accumulate, each new
# placement raycasts against all previously imported geometry, so the drag
# gets progressively laggier.  We cache a per-mesh uniform-grid acceleration
# structure (keyed by DAG path) built once per drag, and skip meshes whose
# world-space bounding box the ray never enters.  The cache is cleared at the
# start of every drag so freshly imported / edited meshes are re-accelerated.
_isect_accel_cache: dict = {}


def _clear_raycast_cache() -> None:
    """Drop cached intersection accelerators (call on each drag start)."""
    _isect_accel_cache.clear()


def _ray_hits_aabb(ox, oy, oz, dx, dy, dz, bmin, bmax) -> bool:
    """Slab test: does the ray (origin + t·dir, t≥0) intersect the AABB?

    ``dx/dy/dz`` need not be normalised.  Returns True on any hit or if the
    ray origin is already inside the box.
    """
    tmin = 0.0
    tmax = float("inf")
    for o, d, lo, hi in (
        (ox, dx, bmin[0], bmax[0]),
        (oy, dy, bmin[1], bmax[1]),
        (oz, dz, bmin[2], bmax[2]),
    ):
        if abs(d) < 1e-12:
            # Ray parallel to this slab — miss if origin is outside it.
            if o < lo or o > hi:
                return False
            continue
        inv = 1.0 / d
        t1 = (lo - o) * inv
        t2 = (hi - o) * inv
        if t1 > t2:
            t1, t2 = t2, t1
        tmin = max(tmin, t1)
        tmax = min(tmax, t2)
        if tmin > tmax:
            return False
    return tmax >= 0.0


def _ray_aabb_hit(ox, oy, oz, dx, dy, dz, bmin, bmax):
    """Ray/AABB intersection returning ``(t_enter, face_normal)`` or ``None``.

    ``t_enter`` is the distance along the (normalised) ray direction to the
    first face struck; ``face_normal`` is that face's outward normal. Used to
    project the placement helper onto USD-stage bounding boxes, which have no
    Maya mesh to intersect.
    """
    tmin = 0.0
    tmax = float("inf")
    hit_axis = 0
    hit_sign = -1.0
    for axis, (o, d, lo, hi) in enumerate(
        (
            (ox, dx, bmin[0], bmax[0]),
            (oy, dy, bmin[1], bmax[1]),
            (oz, dz, bmin[2], bmax[2]),
        )
    ):
        if abs(d) < 1e-12:
            if o < lo or o > hi:
                return None
            continue
        inv = 1.0 / d
        t1 = (lo - o) * inv
        t2 = (hi - o) * inv
        sign = -1.0  # entering through the low (min) face
        if t1 > t2:
            t1, t2 = t2, t1
            sign = 1.0  # entering through the high (max) face
        if t1 > tmin:
            tmin = t1
            hit_axis = axis
            hit_sign = sign
        tmax = min(tmax, t2)
        if tmin > tmax:
            return None
    if tmax < 0.0:
        return None
    normal = [0.0, 0.0, 0.0]
    normal[hit_axis] = hit_sign
    return tmin, (normal[0], normal[1], normal[2])


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
                    lParam,
                    ctypes.POINTER(_MSLLHOOKSTRUCT),
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
    # Windows-only: on macOS / Linux Qt's event filter handles wheel/button
    # events directly, and ``ctypes.WinDLL`` isn't available, so skip entirely.
    if not _IS_WINDOWS:
        return
    import threading

    def _hook_thread_main():
        global _hook_handle, _hook_proc_ref, _hook_installed
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            HOOKPROC = ctypes.WINFUNCTYPE(
                ctypes.c_long,
                ctypes.c_int,
                wintypes.WPARAM,
                wintypes.LPARAM,
            )
            _hook_proc_ref = HOOKPROC(_ll_mouse_proc)
            user32.SetWindowsHookExW.restype = wintypes.HHOOK
            user32.SetWindowsHookExW.argtypes = [
                ctypes.c_int,
                HOOKPROC,
                wintypes.HINSTANCE,
                wintypes.DWORD,
            ]
            user32.GetMessageW.argtypes = [
                ctypes.c_void_p,
                wintypes.HWND,
                wintypes.UINT,
                wintypes.UINT,
            ]
            user32.GetMessageW.restype = ctypes.c_int

            # NB: without an explicit restype ctypes assumes ``c_int`` (32-bit)
            # and truncates the 64-bit HMODULE returned here, yielding an
            # invalid handle → SetWindowsHookExW fails with ERROR_MOD_NOT_FOUND
            # (126) on machines where user32 loads at a high address.
            kernel32.GetModuleHandleW.restype = wintypes.HMODULE
            kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]

            hmod = kernel32.GetModuleHandleW(None)
            _hook_handle = user32.SetWindowsHookExW(_WH_MOUSE_LL, _hook_proc_ref, hmod, 0)
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
                "Low-level mouse hook installed on dedicated thread (hhook=%s, tid=%s).",
                _hook_handle,
                threading.get_ident(),
            )

            # Pump messages until the thread exits (process shutdown).
            msg = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        except Exception:
            log.exception("Hook thread crashed:")

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

        return om2.MDistance(1.0, om2.MDistance.kMeters).asUnits(om2.MDistance.internalUnit())
    except Exception:
        return 100.0


# ═════════════════════════════════════════════════════════════════════════════
# Placement state  (module-level, queried by the draw override)
# ═════════════════════════════════════════════════════════════════════════════


@dataclass
class _State:
    asset_data: dict[str, Any] = field(default_factory=dict)
    thumb_path: str = ""
    # bbox in object-local space (matches Blendkit API bbox_min/bbox_max)
    bbox_min: tuple[float, float, float] = (-0.5, 0.0, -0.5)
    bbox_max: tuple[float, float, float] = (0.5, 1.0, 0.5)
    # current world placement
    location: tuple[float, float, float] = (0.0, 0.0, 0.0)
    rotation_y: float = 0.0  # degrees, controlled by mouse wheel
    # hit status
    has_hit: bool = False
    hit_floor: bool = False
    active: bool = False
    # world-space surface normal at the raycast hit; (0,1,0) when the
    # cursor is on the floor plane or no surface was struck.
    surface_normal: tuple[float, float, float] = (0.0, 1.0, 0.0)
    # ── Material drop mode ─────────────────────────────────────────────
    # Materials are dragged straight onto an existing mesh (no bounding-box
    # helper). ``is_material`` switches the drag session into mesh-pick mode
    # and ``target_mesh`` holds the DAG path of the mesh currently under the
    # cursor (empty when the cursor is not over a mesh).
    is_material: bool = False
    target_mesh: str = ""
    # ── HDRI drop mode ─────────────────────────────────────────────────
    # HDRIs become an environment / dome light. They can be dropped anywhere
    # in the viewport (no mesh target, no bounding-box helper) and only the
    # cursor-following badge is shown during the drag.
    is_hdri: bool = False
    # proxor line data (may be empty list)
    proxor_lines: list[list[tuple[float, float, float]]] = field(default_factory=list)
    # proxor hologram mesh (flat list of triangle vertices, 3 per tri,
    # already in Maya local space; may be empty)
    proxor_mesh: list[tuple[float, float, float]] = field(default_factory=list)


# The active state — read by the draw override every frame.
_active_state: _State = _State()


def _is_material_asset(asset_data: dict[str, Any]) -> bool:
    """Return True when *asset_data* is a Blendkit material asset.

    Material assets are applied directly to an existing mesh under the
    cursor instead of being placed with a bounding-box helper.
    """
    return str(asset_data.get("assetType") or "").lower() == "material"


def _is_hdri_asset(asset_data: dict[str, Any]) -> bool:
    """Return True when *asset_data* is a Blendkit HDRI asset.

    HDRIs are dropped anywhere in the viewport and turned into an
    environment / dome light instead of placed as geometry.
    """
    return str(asset_data.get("assetType") or "").lower() in ("hdr", "hdri")


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


def _widget_alive(w: QWidget | None) -> bool:
    """True if the C++ object behind a QWidget wrapper still exists.

    Maya can destroy the viewport's underlying C++ widget (e.g. when a
    panel is torn down or rebuilt) while a stale Python wrapper lingers.
    Touching such a wrapper raises ``RuntimeError: Internal C++ object
    already deleted``.  Callers use this to drop the stale cache and
    re-resolve the current viewport instead of crashing.
    """
    if w is None:
        return False
    for sh_name in ("shiboken6", "shiboken2"):
        try:
            sh = __import__(sh_name)
            return bool(sh.isValid(w))
        except ImportError:
            continue
        except Exception:
            break
    # Fallback when shiboken isn't importable: poke a cheap accessor.
    try:
        w.objectName()
        return True
    except RuntimeError:
        return False
    except Exception:
        return True


def _get_viewport_widget() -> QWidget | None:
    """Return the QWidget for Maya's active 3D viewport.

    Tries four strategies; logs at debug which one won.
    """
    try:
        import maya.OpenMayaUI as omui1

        view = omui1.M3dView.active3dView()
        ptr = view.widget()

        if isinstance(ptr, QWidget):  # type: ignore
            return ptr

        if ptr is not None and int(ptr) != 0:
            for sh_name in ("shiboken6", "shiboken2"):
                try:
                    sh = __import__(sh_name)
                    return sh.wrapInstance(int(ptr), QWidget)
                except Exception:
                    log.exception("Failed to wrap M3dView widget with %s:", sh_name)
    except Exception as e:
        log.debug("BK viewport: M3dView.widget() error: %s", e)

    try:
        import maya.OpenMayaUI as omui1
        from maya import cmds

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
                            return sh.wrapInstance(int(ptr), QWidget)  # type: ignore
                        except Exception:
                            log.exception("Failed to wrap modelPanel widget with %s:", sh_name)
            except Exception:
                log.exception("Error processing modelPanel %s:", panel)
    except Exception:
        log.exception("BK viewport: modelPanel query error:")

    best, best_area = None, 0
    for w in QApplication.allWidgets():
        if not w.isVisible():
            continue
        cn = w.metaObject().className() if w.metaObject() else ""
        if any(
            kw in cn
            for kw in (
                "GLWidget",
                "MayaGL",
                "THoverQ",
                "modelEditor",
                "MayaViewport",
                "ViewportUI",
            )
        ):
            area = w.width() * w.height()
            if area > best_area:
                best, best_area = w, area
    return best


def _raycast_scene(vp_x: int, vp_y: int) -> tuple[bool, tuple, tuple, bool, str]:
    """Cast a ray from viewport pixel (vp_x, vp_y) (Qt coords).

    Returns ``(has_hit, (x,y,z), (nx,ny,nz), hit_floor, hit_node)``.
    ``hit_floor`` is True when the Y=0 fallback plane was used because no
    scene geometry was struck; the normal is then ``(0,1,0)``.  When no hit
    at all (ray parallel to the floor) the normal is also ``(0,1,0)``.
    ``hit_node`` is the full DAG path of the mesh shape that was struck
    (empty string for the floor fallback / no hit) — used by material
    drag-drop to pick an assignment target.
    """
    floor_normal = (0.0, 1.0, 0.0)
    try:
        import maya.api.OpenMaya as om2
        import maya.api.OpenMayaUI as omui2

        view = omui2.M3dView.active3dView()
        vp_h = view.portHeight()
        maya_y = vp_h - vp_y  # Qt top-down → Maya bottom-up

        near_pt = om2.MPoint()
        far_pt = om2.MPoint()
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
        best_normal: tuple | None = None
        best_node: str = ""

        it = om2.MItDag(om2.MItDag.kDepthFirst, om2.MFn.kMesh)
        while not it.isDone():
            try:
                dag_path = it.getPath()
                if not dag_path.isVisible():
                    it.next()
                    continue

                # ── Broadphase: skip meshes whose world AABB the ray misses.
                # A cheap ray/AABB slab test rejects the vast majority of
                # already-placed models (they sit away from the cursor ray),
                # so we never pay for a full mesh intersection on them.
                dag_key = dag_path.fullPathName()
                try:
                    fn_dag = om2.MFnDagNode(dag_path)
                    obb = fn_dag.boundingBox
                    obb.transformUsing(dag_path.inclusiveMatrix())
                    bmn = obb.min
                    bmx = obb.max
                    if not _ray_hits_aabb(
                        ray_src.x,
                        ray_src.y,
                        ray_src.z,
                        ray_dir.x,
                        ray_dir.y,
                        ray_dir.z,
                        (bmn.x, bmn.y, bmn.z),
                        (bmx.x, bmx.y, bmx.z),
                    ):
                        it.next()
                        continue
                except Exception:
                    pass  # If the AABB test fails, fall through to full test.

                fn = om2.MFnMesh(dag_path)

                # ── Cached uniform-grid accelerator for this mesh ──────────
                accel = _isect_accel_cache.get(dag_key)
                if accel is None:
                    try:
                        accel = fn.autoUniformGridParams()
                        _isect_accel_cache[dag_key] = accel
                    except Exception:
                        accel = None

                try:
                    result = fn.closestIntersection(
                        ray_src,
                        ray_dir,
                        om2.MSpace.kWorld,
                        9999999.0,  # maxParam
                        False,  # testBothDirections
                        accelParams=accel,
                    )
                except Exception:
                    # Older bindings may not accept the keyword — retry plain.
                    result = fn.closestIntersection(
                        ray_src,
                        ray_dir,
                        om2.MSpace.kWorld,
                        9999999.0,
                        False,
                    )
                if result is not None:
                    hit_pt = result[0]
                    hit_face = int(result[2]) if len(result) > 2 else -1
                    dx = hit_pt.x - near_pt.x
                    dy = hit_pt.y - near_pt.y
                    dz = hit_pt.z - near_pt.z
                    d = math.sqrt(dx * dx + dy * dy + dz * dz)
                    if 0.001 < d < closest_dist:
                        closest_dist = d
                        best_hit = (float(hit_pt.x), float(hit_pt.y), float(hit_pt.z))
                        try:
                            best_node = dag_path.fullPathName()
                        except Exception:
                            best_node = ""
                        nrm = None
                        if hit_face >= 0:
                            try:
                                nv = fn.getPolygonNormal(hit_face, om2.MSpace.kWorld)
                                nrm = (float(nv.x), float(nv.y), float(nv.z))
                            except Exception:
                                nrm = None
                        if nrm is None:
                            try:
                                nv, _face = fn.getClosestNormal(
                                    om2.MPoint(*best_hit),
                                    om2.MSpace.kWorld,
                                )
                                nrm = (float(nv.x), float(nv.y), float(nv.z))
                            except Exception:
                                nrm = floor_normal
                        # Flip the normal toward the camera so back-face
                        # hits don't invert the asset.
                        if (nrm[0] * ray_dir.x + nrm[1] * ray_dir.y + nrm[2] * ray_dir.z) > 0.0:
                            nrm = (-nrm[0], -nrm[1], -nrm[2])
                        # Normalize defensively.
                        ln = math.sqrt(nrm[0] ** 2 + nrm[1] ** 2 + nrm[2] ** 2) or 1.0
                        best_normal = (nrm[0] / ln, nrm[1] / ln, nrm[2] / ln)
            except Exception:
                pass
            it.next()

        # ── USD stages (mayaUsdProxyShape) ────────────────────────────────
        # Staged assets (import method = "stage") are proxy shapes, not Maya
        # meshes, so the MItDag(kMesh) loop above never sees them.  Project the
        # ray onto each stage's world bounding box so the placement helper can
        # still land on top of / against staged geometry.  Bounding-box level
        # (not surface-exact) but fast and keeps "both at the same time".
        try:
            import maya.cmds as _cmds

            proxies = _cmds.ls(type="mayaUsdProxyShape", long=True) or []
        except Exception:
            proxies = []
        for shape in proxies:
            try:
                sel = om2.MSelectionList()
                sel.add(shape)
                dag_path = sel.getDagPath(0)
                if not dag_path.isVisible():
                    continue
                fn_dag = om2.MFnDagNode(dag_path)
                obb = fn_dag.boundingBox
                obb.transformUsing(dag_path.inclusiveMatrix())
                bmn = obb.min
                bmx = obb.max
                hit = _ray_aabb_hit(
                    ray_src.x,
                    ray_src.y,
                    ray_src.z,
                    ray_dir.x,
                    ray_dir.y,
                    ray_dir.z,
                    (bmn.x, bmn.y, bmn.z),
                    (bmx.x, bmx.y, bmx.z),
                )
                if hit is None:
                    continue
                t_enter, nrm = hit
                if not (0.001 < t_enter < closest_dist):
                    continue
                closest_dist = t_enter
                best_hit = (
                    float(near_pt.x + t_enter * ray_dir.x),
                    float(near_pt.y + t_enter * ray_dir.y),
                    float(near_pt.z + t_enter * ray_dir.z),
                )
                best_node = shape
                # Flip the face normal toward the camera so a side/bottom hit
                # doesn't invert the placed asset.
                if (nrm[0] * ray_dir.x + nrm[1] * ray_dir.y + nrm[2] * ray_dir.z) > 0.0:
                    nrm = (-nrm[0], -nrm[1], -nrm[2])
                best_normal = nrm
            except Exception:
                continue

        if best_hit:
            return True, best_hit, (best_normal or floor_normal), False, best_node

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
                return True, (ox + t * dx, 0.0, oz + t * dz), floor_normal, True, ""

        # No hit and the ray is parallel to the floor — project the camera
        # eye-line forward by an arbitrary distance so the bbox is still
        # visible at the cursor depth instead of snapping back to origin.
        t = 1000.0  # 10 m in Maya cm units; comfortably within view
        return False, (ox + t * dx, oy + t * dy, oz + t * dz), floor_normal, False, ""

    except Exception as exc:
        log.debug("Raycast error: %s", exc)

    return False, (0.0, 0.0, 0.0), floor_normal, False, ""


def _project_cursor_to_floor(vp_x: int, vp_y: int) -> tuple[float, float, float]:
    """Project the cursor ray onto the Y=0 floor plane, ignoring geometry.

    Used by the Alt "drop to floor" placement mode. Returns the world point
    where the eye ray through pixel (vp_x, vp_y) crosses Y=0; if the ray is
    parallel to the floor, projects the eye-line forward instead.
    """
    try:
        import maya.api.OpenMaya as om2
        import maya.api.OpenMayaUI as omui2

        view = omui2.M3dView.active3dView()
        vp_h = view.portHeight()
        maya_y = vp_h - vp_y  # Qt top-down → Maya bottom-up

        near_pt = om2.MPoint()
        far_pt = om2.MPoint()
        view.viewToWorld(int(vp_x), int(maya_y), near_pt, far_pt)

        ox, oy, oz = near_pt.x, near_pt.y, near_pt.z
        dx = far_pt.x - near_pt.x
        dy = far_pt.y - near_pt.y
        dz = far_pt.z - near_pt.z
        if abs(dy) > 1e-9:
            t = -oy / dy
            if t > 0:
                return (ox + t * dx, 0.0, oz + t * dz)
        # Ray parallel to the floor — project forward so the helper stays visible.
        t = 1000.0
        return (ox + t * dx, oy + t * dy, oz + t * dz)
    except Exception as exc:
        log.debug("floor projection failed: %s", exc)
        return (0.0, 0.0, 0.0)


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
# Locator node lifecycle (transient - created on drag start, deleted on end)
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
            here = os.path.dirname(os.path.abspath(__file__))  # …/bk_maya/ui
            bk_maya_dir = os.path.dirname(here)  # …/bk_maya
            plugin_path = os.path.join(bk_maya_dir, "plugins", "placement_locator.py")
            log.warning(
                "bkPlacementLocator not registered - attempting to load %s",
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
            if shapes:  # noqa: SIM108
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
# Proxor loader (optional - falls back to bbox draw when absent)
# ═════════════════════════════════════════════════════════════════════════════


def _proxor_cache_path(asset_data: dict[str, Any]) -> str:
    """Return the on-disk path where this asset's ``.prxc`` should live.

    Mirrors the Blender add-on convention:
    ``<global_dir>/tmp/<asset_type>_search/<assetBaseId>.prxc``.
    """
    asset_base_id = asset_data.get("assetBaseId", "")
    if not asset_base_id:
        return ""
    asset_type = asset_data.get("assetType", "model") or "model"
    try:
        from bk_maya.core import prefs as _prefs_mod

        base = _prefs_mod.prefs.global_dir_resolved()
    except Exception:
        base = os.path.expanduser("~/blenderkit_data")
    return os.path.join(base, "tmp", f"{asset_type}_search", f"{asset_base_id}.prxc")


def _prxc_download_url(asset_data: dict[str, Any]) -> str:
    """Pluck the signed ``.prxc`` download URL out of ``files[]`` (if any)."""
    for f in asset_data.get("files") or []:
        if (f.get("fileType") == "prxc") and f.get("downloadUrl"):
            return str(f["downloadUrl"])
    return ""


def _load_proxor_payload(asset_data: dict[str, Any]) -> dict[str, Any]:
    """Return ``{"lines": [...], "mesh": [...]}`` parsed from local cache.

    Empty values mean the ``.prxc`` isn't on disk yet — the drag/click
    session will then trigger an async download via
    :meth:`DragSession._start_proxor_fetch`.
    """
    asset_base_id = asset_data.get("assetBaseId", "")
    if not asset_base_id:
        return {"lines": [], "mesh": []}

    # Primary location matches the path we'll ask the client to download to.
    candidates: list[str] = []
    primary = _proxor_cache_path(asset_data)
    if primary:
        candidates.append(primary)

    # Backwards-compat lookups in older cache layouts.
    try:
        from bk_maya.core import global_vars as gv  # type: ignore

        for attr in ("CACHE_DIR", "PROXOR_DIR", "BLENDKIT_DATA_DIR"):
            p = getattr(gv, attr, None)
            if p:
                candidates.append(os.path.join(str(p), "proxors", f"{asset_base_id}.prxc"))
    except Exception:
        pass
    candidates.append(os.path.expanduser(f"~/blenderkit_data/proxors/{asset_base_id}.prxc"))

    prxc_path = ""
    for c in candidates:
        if os.path.isfile(c):
            prxc_path = c
            break
    if not prxc_path:
        return {"lines": [], "mesh": []}

    return _parse_prxc(prxc_path)


# Back-compat shim — some external callers/tests may still use the old name.
def _load_proxor_lines(asset_data: dict[str, Any]) -> list[list[tuple]]:
    return _load_proxor_payload(asset_data).get("lines", [])


def _parse_prxc(prxc_path: str) -> dict[str, Any]:
    """Read a ``.prxc`` from disk and return both lines and mesh data.

    Returns ``{"lines": [...polylines...], "mesh": [...flat tri verts...]}``
    in Maya local space (axis-swapped, scaled to internal units).
    """
    out: dict[str, Any] = {"lines": [], "mesh": []}
    try:
        from bk_maya.bk_proxor import prx_format as pf
        from bk_maya.bk_proxor._maya.draw import (
            prx_to_line_segments,
            prx_to_mesh_triangles,
        )
    except Exception as exc:
        log.debug("bk_proxor unavailable: %s", exc)
        return out
    try:
        payload = pf.read_prx(prxc_path)
        scale = _meters_to_internal()
        out["lines"] = prx_to_line_segments(payload, world_scale=scale, axis_swap_yz=False)
        out["mesh"] = prx_to_mesh_triangles(payload, world_scale=scale, axis_swap_yz=False)
        log.debug("Proxor loaded from %s: %d segments, %d mesh verts", prxc_path, len(out["lines"]), len(out["mesh"]))
    except Exception as exc:
        log.debug("Proxor parse failed for %s: %s", prxc_path, exc)
    return out


# Back-compat shim.
def _parse_prxc_to_segments(prxc_path: str) -> list[list[tuple]]:
    return _parse_prxc(prxc_path).get("lines", [])


def _start_proxor_fetch_for_locator(asset_data: dict[str, Any], locator_name: str) -> None:
    """Async ``.prxc`` fetch whose completion callback writes directly to
    *locator_name* via ``locator_state``. Used by ``place_at_origin`` so
    the proxor swap-in does not depend on ``DragSession`` being active.
    """
    asset_type = asset_data.get("assetType")
    if asset_type not in ("model", "printable"):
        return
    url = _prxc_download_url(asset_data)
    if not url:
        return
    path = _proxor_cache_path(asset_data)
    if not path:
        return
    asset_base_id = asset_data.get("assetBaseId", "")
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
    except OSError as exc:
        log.debug("Could not create proxor cache dir: %s", exc)
        return
    try:
        from bk_maya.core import auth, client_lib
        from bk_maya.core import locator_state as plc
    except Exception as exc:
        log.debug("client_lib unavailable for proxor fetch: %s", exc)
        return

    def _on_ready(prxc_path: str) -> None:
        try:
            payload = _parse_prxc(prxc_path)
        except Exception as exc:
            log.debug("Proxor swap-in parse failed: %s", exc)
            return
        try:
            plc.set_proxor_lines(locator_name, list(payload.get("lines", [])))
            plc.set_proxor_mesh(locator_name, list(payload.get("mesh", [])))
            _refresh_viewport(light=True)
            log.info(
                "[PROXOR] locator-bound swap-in: %d segments, %d mesh-verts",
                len(payload.get("lines", [])),
                len(payload.get("mesh", [])),
            )
        except Exception as exc:
            log.debug("locator-bound proxor publish failed: %s", exc)

    try:
        client_lib.prxc_registry.register(asset_base_id, _on_ready)
    except Exception as exc:
        log.debug("prxc_registry.register failed: %s", exc)
        return

    def _worker() -> None:
        try:
            client_lib.ensure_running()
            api_key = ""
            try:
                api_key = auth.get_api_key() or ""
            except Exception:
                pass
            task_id = client_lib.asset_prxc_download(
                asset_base_id=asset_base_id,
                download_url=url,
                file_path=path,
                api_key=api_key,
            )
            log.info("[PROXOR] (locator) download scheduled task=%s -> %s", task_id, path)
        except Exception as exc:
            log.debug("asset_prxc_download failed: %s", exc)
            try:
                client_lib.prxc_registry.unregister(asset_base_id)
            except Exception:
                pass

    import threading

    threading.Thread(
        target=_worker,
        name=f"bk-prxc-fetch-{asset_base_id[:8]}",
        daemon=True,
    ).start()


# ═════════════════════════════════════════════════════════════════════════════
# Thumbnail cursor
# ═════════════════════════════════════════════════════════════════════════════

_CURSOR_SIZE = 64


def _make_cursor(thumb_path: str) -> QCursor:
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
            _CURSOR_SIZE,
            _CURSOR_SIZE,
            Qt.KeepAspectRatio,
            Qt.SmoothTransformation,
        )
    return QCursor(pix, 0, 0)


class _DragOverlay(QWidget):  # type: ignore
    """Frameless, click-through badge that follows the cursor during a drag.

    Maya's 3D viewport is a native GL surface, and on macOS the Qt override
    cursor isn't reliably painted over it — so material drags (which have no
    3D locator) had no visible "you are dragging" indicator. This is an
    independent always-on-top window we reposition on every cursor poll. Its
    accent colour also signals whether the mesh under the cursor is a valid
    drop target (green) or not (neutral).
    """

    _BADGE = 64  # px — thumbnail box
    _PAD = 6  # px — card padding / border room

    def __init__(self, thumb_path: str = "") -> None:
        super().__init__(None)
        flags = Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint
        no_focus = getattr(Qt, "WindowDoesNotAcceptFocus", None)
        if no_focus is not None:
            flags |= no_focus
        transparent = getattr(Qt, "WindowTransparentForInput", None)
        if transparent is not None:
            flags |= transparent
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.NoFocus)
        self._valid = False
        self._pix = QPixmap()
        if thumb_path and os.path.isfile(thumb_path):
            src = QPixmap(thumb_path)
            if not src.isNull():
                self._pix = src.scaled(self._BADGE, self._BADGE, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        side = self._BADGE + self._PAD * 2
        self.resize(side, side)

    def set_valid(self, valid: bool) -> None:
        if valid != self._valid:
            self._valid = valid
            self.update()

    def move_to(self, gp) -> None:
        # Sit just below-right of the cursor tip so it never hides the hit point.
        self.move(int(gp.x()) + 16, int(gp.y()) + 16)

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.Antialiasing, True)
            rect = self.rect().adjusted(1, 1, -1, -1)
            accent = QColor(0, 220, 120, 235) if self._valid else QColor(235, 235, 235, 205)
            p.setBrush(QColor(25, 25, 25, 185))
            p.setPen(QPen(accent, 2))
            p.drawRoundedRect(rect, 10, 10)
            if not self._pix.isNull():
                x = (self.width() - self._pix.width()) // 2
                y = (self.height() - self._pix.height()) // 2
                p.drawPixmap(x, y, self._pix)
            else:
                # Fallback crosshair when no thumbnail is available.
                m = self.width() // 2
                p.drawLine(m, self._PAD + 4, m, self.height() - self._PAD - 4)
                p.drawLine(self._PAD + 4, m, self.width() - self._PAD - 4, m)
        finally:
            p.end()


class _ProgressOverlay(QWidget):  # type: ignore
    """Frameless, click-through progress card pinned to the viewport top.

    Used for downloads that have no 3D gizmo to carry their progress text
    (HDRIs become world-level environment lights). Unlike ``inViewMessage``
    this does not depend on Maya's "In-view Messages" HUD preference, so it
    is always visible while a download runs.
    """

    _MIN_W = 240  # px — minimum card width
    _H = 52
    _H_PAD = 22  # px — horizontal text padding (each side)

    def __init__(self) -> None:
        super().__init__(None)
        flags = Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint
        no_focus = getattr(Qt, "WindowDoesNotAcceptFocus", None)
        if no_focus is not None:
            flags |= no_focus
        transparent = getattr(Qt, "WindowTransparentForInput", None)
        if transparent is not None:
            flags |= transparent
        self.setWindowFlags(flags)
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.NoFocus)
        self.resize(self._MIN_W, self._H)
        self._text = ""
        self._frac: float | None = None

    def set_progress(self, text: str, frac: float | None = None) -> None:
        self._text = text or ""
        self._frac = frac
        # Grow the card to fit the full text so nothing is clipped.
        fm = self.fontMetrics()
        try:
            tw = fm.horizontalAdvance(self._text)
        except AttributeError:  # Qt < 5.11
            tw = fm.width(self._text)
        new_w = max(self._MIN_W, tw + self._H_PAD * 2)
        if new_w != self.width():
            self.resize(new_w, self._H)
        self.update()

    def reposition(self) -> None:
        vp = _get_viewport_widget()
        if vp is None:
            return
        try:
            gp = vp.mapToGlobal(QPoint(vp.width() // 2, 44))
            self.move(int(gp.x()) - self.width() // 2, int(gp.y()))
        except Exception:
            pass

    def paintEvent(self, event) -> None:
        p = QPainter(self)
        try:
            p.setRenderHint(QPainter.Antialiasing, True)
            rect = self.rect().adjusted(1, 1, -1, -1)
            p.setBrush(QColor(25, 25, 25, 210))
            p.setPen(QPen(QColor(41, 107, 214, 235), 2))
            p.drawRoundedRect(rect, 10, 10)
            # Determinate progress bar along the bottom edge.
            if self._frac is not None:
                frac = max(0.0, min(1.0, self._frac))
                bar = rect.adjusted(8, rect.height() - 10, -8, -6)
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(70, 70, 70, 200))
                p.drawRoundedRect(bar, 3, 3)
                if frac > 0.0:
                    fill = bar.adjusted(0, 0, -int(bar.width() * (1.0 - frac)), 0)
                    p.setBrush(QColor(41, 107, 214, 255))
                    p.drawRoundedRect(fill, 3, 3)
            p.setPen(QPen(QColor(235, 235, 235, 255)))
            p.drawText(rect.adjusted(12, 0, -12, -8 if self._frac is not None else 0), Qt.AlignCenter, self._text)
        finally:
            p.end()


_progress_overlay: _ProgressOverlay | None = None


def show_progress(text: str, frac: float | None = None) -> None:
    """Show/update the viewport progress card (creates it on first call)."""
    if not _QT:
        return
    global _progress_overlay
    try:
        if _progress_overlay is None:
            _progress_overlay = _ProgressOverlay()
        _progress_overlay.set_progress(text, frac)
        _progress_overlay.reposition()
        if not _progress_overlay.isVisible():
            _progress_overlay.show()
        _progress_overlay.raise_()
    except Exception as exc:
        log.debug("show_progress failed: %s", exc)


def hide_progress() -> None:
    """Hide the viewport progress card if it is showing."""
    global _progress_overlay
    if _progress_overlay is not None:
        try:
            _progress_overlay.hide()
        except Exception:
            pass


# ═════════════════════════════════════════════════════════════════════════════
# Drag session (singleton Qt event filter)
# ═════════════════════════════════════════════════════════════════════════════


class DragSession(QObject):  # type: ignore
    """Qt event filter driving a single drag-to-place operation."""

    _instance: DragSession | None = None

    @classmethod
    def get(cls) -> DragSession:
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        super().__init__()
        self._vp_widget: QWidget | None = None
        self._locator_name: str | None = None
        self._download_started = False
        self._poll_timer: QTimer | None = None  # polls cursor position
        # Material-drag highlight bookkeeping (the mesh under the cursor is
        # selected so the user sees what will receive the material).
        self._orig_selection: list[str] = []
        self._hilited_mesh: str = ""
        # Cursor-following badge shown during material drags.
        self._overlay: _DragOverlay | None = None

    # ── Public API ────────────────────────────────────────────────────────

    def start(self, asset_data: dict[str, Any], thumb_path: str, delay_locator: bool = False) -> None:
        global _active_state
        if _active_state.active:
            log.debug("DragSession already active; ignoring start()")
            return
        self._delay_locator = delay_locator
        self._drop_fired = False
        # Rebuild the raycast accelerator cache for this drag so any meshes
        # imported by previous placements are re-accelerated (and stale
        # entries dropped).
        _clear_raycast_cache()
        # Materials are applied to an existing mesh under the cursor — no
        # bounding-box helper / proxor wireframe is shown for them.
        is_material = _is_material_asset(asset_data)
        is_hdri = _is_hdri_asset(asset_data)
        if is_material:
            # Never spawn or lazily create the placement locator in material
            # mode; the cursor-thumbnail + mesh highlight is the only UI.
            self._delay_locator = False
            # Remember the current selection so the hover-highlight can be
            # restored when the drag ends / is cancelled.
            self._orig_selection = []
            self._hilited_mesh = ""
            try:
                import maya.cmds as cmds

                self._orig_selection = cmds.ls(selection=True, long=True) or []
            except Exception:
                self._orig_selection = []
        elif is_hdri:
            # HDRIs are world-level environment lights — no locator, no mesh
            # target. Only the cursor-following badge is shown.
            self._delay_locator = False
        # Blendkit API returns bbox in meters; Maya internal unit is cm.
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
                return (float(seq[0]) * scale, float(seq[1]) * scale, float(seq[2]) * scale)
            except (TypeError, ValueError, IndexError):
                return default

        default_min = (-0.5 * scale, 0.0, -0.5 * scale)
        default_max = (0.5 * scale, 1.0 * scale, 0.5 * scale)

        # The Blendkit search endpoint returns the bbox split across six
        # scalar params under ``dictParameters`` — ``boundBoxMinX/Y/Z`` and
        # ``boundBoxMaxX/Y/Z`` (all in meters, Blender Z-up).  The Blender
        # add-on folds those into top-level ``bbox_min`` / ``bbox_max``
        # tuples during search-result parsing (see addon's search.py); the
        # Maya port doesn't (yet), so do it here so dragging any real asset
        # gets its real bounding box instead of the 1 m³ default cube.
        raw_min = asset_data.get("bbox_min")
        raw_max = asset_data.get("bbox_max")
        if raw_min is None or raw_max is None:
            params = asset_data.get("dictParameters") or asset_data.get("parameters") or {}
            try:
                mn = (float(params["boundBoxMinX"]), float(params["boundBoxMinY"]), float(params["boundBoxMinZ"]))
                mx = (float(params["boundBoxMaxX"]), float(params["boundBoxMaxY"]), float(params["boundBoxMaxZ"]))
                if raw_min is None:
                    raw_min = mn
                if raw_max is None:
                    raw_max = mx
                log.info("bbox extracted from dictParameters: min=%s max=%s", mn, mx)
            except (KeyError, TypeError, ValueError):
                log.warning("Asset %s has no usable bbox - using 1 m default cube", asset_data.get("name", "?"))

        bbox_min = _coerce_bbox(raw_min, default_min)
        bbox_max = _coerce_bbox(raw_max, default_max)

        # Maya is Y-up; Blendkit/Blender is Z-up. Swap Y/Z so the asset's
        # "up" axis points up in Maya, then re-order min/max per-axis.
        sw_min = (bbox_min[0], bbox_min[2], bbox_min[1])
        sw_max = (bbox_max[0], bbox_max[2], bbox_max[1])
        bbox_min = tuple(min(sw_min[i], sw_max[i]) for i in range(3))
        bbox_max = tuple(max(sw_min[i], sw_max[i]) for i in range(3))

        _prxc = _load_proxor_payload(asset_data)
        _active_state = _State(
            asset_data=asset_data,
            thumb_path=thumb_path,
            bbox_min=bbox_min,
            bbox_max=bbox_max,
            location=(0.0, 0.0, 0.0),
            rotation_y=0.0,
            active=True,
            is_material=is_material,
            is_hdri=is_hdri,
            proxor_lines=_prxc.get("lines", []),
            proxor_mesh=_prxc.get("mesh", []),
        )
        log.info(
            "Drag start: asset=%s bbox_min=%s bbox_max=%s",
            asset_data.get("name", "?"),
            bbox_min,
            bbox_max,
        )

        # Spawn the locator (draw override starts ticking).  Material and HDRI
        # drops never use a locator — materials highlight an existing mesh and
        # HDRIs become a world-level environment light.
        if is_material or is_hdri:
            self._locator_name = None
        else:
            self._locator_name = None if delay_locator else _create_locator()
            if self._locator_name is None and not delay_locator:
                log.warning(
                    "bkPlacementLocator node type not registered - load the Blendkit plugin via Plug-in Manager."
                )
            elif self._locator_name is not None:
                self._publish_bbox_to_locator()
                self._publish_proxor_to_locator()

            # If the .prxc wasn't on disk yet, ask the local client to fetch
            # it now; when it lands the registry callback swaps the bbox out
            # for the wireframe mid-drag (Blender-style).
            if not _active_state.proxor_lines and not _active_state.proxor_mesh:
                self._start_proxor_fetch(asset_data)

        # Override cursor with thumbnail
        QApplication.setOverrideCursor(_make_cursor(thumb_path))

        # Material drags have no 3D locator and the override cursor above
        # isn't painted over Maya's native GL viewport on macOS, so spawn an
        # independent cursor-following badge as the "you are dragging" cue.
        # HDRIs use the same badge (they have no locator either).
        self._overlay = None
        if is_material or is_hdri:
            try:
                self._overlay = _DragOverlay(thumb_path)
                self._overlay.move_to(QCursor.pos())
                self._overlay.show()
                self._overlay.raise_()
            except Exception as exc:
                log.debug("Could not create drag overlay: %s", exc)
                self._overlay = None

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
                # Walk up the ancestor chain - Maya's modelPanel nests the
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
                    for ch in self._vp_widget.findChildren(QWidget):  # type: ignore
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
                self._poll_timer.setInterval(16)  # ms (~60 fps)
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

            if is_material:
                hint = (
                    f"<b>{asset_data.get('name', 'Material')}</b>  "
                    "&nbsp;|&nbsp;  Drop on a mesh to assign  "
                    "&nbsp;|&nbsp;  RMB / ESC: cancel"
                )
            elif is_hdri:
                hint = (
                    f"<b>{asset_data.get('name', 'HDRI')}</b>  "
                    "&nbsp;|&nbsp;  Drop anywhere to add as environment light  "
                    "&nbsp;|&nbsp;  RMB / ESC: cancel"
                )
            else:
                hint = (
                    f"<b>{asset_data.get('name', 'Asset')}</b>  "
                    "&nbsp;|&nbsp;  Wheel: rotate  "
                    "&nbsp;|&nbsp;  Shift: 45&deg; snap  "
                    "&nbsp;|&nbsp;  Ctrl: keep upright  "
                    "&nbsp;|&nbsp;  Alt: drop to floor  "
                    "&nbsp;|&nbsp;  LMB: place  "
                    "&nbsp;|&nbsp;  RMB / ESC: cancel"
                )
            cmds.inViewMessage(
                amg=hint,
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

        # ── Material mode: highlight the mesh under the cursor ────────────
        if _active_state.is_material:
            self._poll_material()
            return

        # ── HDRI mode: just keep the cursor badge following the pointer ───
        if _active_state.is_hdri:
            self._poll_hdri()
            return

        # ── Placement modifier keys ───────────────────────────────────────
        #   Shift → snap rotation to 45° increments (resets to 0 on press)
        #   Ctrl  → place at the surface point but keep the asset upright
        #           (ignore the surface normal)
        #   Alt   → drop straight to the Y=0 floor, ignoring geometry
        try:
            mods = QApplication.keyboardModifiers()
            shift_held = bool(mods & Qt.ShiftModifier)
            ctrl_held = bool(mods & Qt.ControlModifier)
            alt_held = bool(mods & Qt.AltModifier)
        except Exception:
            shift_held = ctrl_held = alt_held = False

        # ── 2. Consume wheel notches from the LL hook ─────────────────────
        # _wheel_accum is updated on the hook thread; one drain per tick.
        # 1 notch = 120 raw units.
        wheel = _drain_wheel_accum()
        rotation_changed = False
        # Shift just pressed → reset rotation to zero, then snap in 45° steps.
        if shift_held and not getattr(self, "_shift_prev", False) and _active_state.rotation_y != 0.0:
            _active_state.rotation_y = 0.0
            rotation_changed = True
        self._shift_prev = shift_held
        if wheel != 0:
            # Wheel-up = positive raw delta on Windows. Negate so wheel-up
            # rotates the gizmo in the natural "away from camera" direction
            # around its local +Y / surface normal.
            step = 45.0 if shift_held else WHEEL_STEP
            _active_state.rotation_y -= (wheel / 120.0) * step
            if shift_held:
                # Snap to the nearest 45° so the increments stay exact.
                _active_state.rotation_y = round(_active_state.rotation_y / 45.0) * 45.0
            rotation_changed = True

        # ── 3. Cursor position → raycast ──────────────────────────────────
        vp = self._vp_widget if _widget_alive(self._vp_widget) else None
        if vp is None:
            vp = _get_viewport_widget()
            self._vp_widget = vp
        if vp is None:
            return

        try:
            gp = QCursor.pos()
        except Exception:
            log.exception("Could not get global cursor position; stopping drag")
            return
        local = vp.mapFromGlobal(gp)
        inside = vp.rect().contains(local)

        # Create the locator on first viewport entry when delay_locator was set.
        if getattr(self, "_delay_locator", False) and self._locator_name is None and inside:
            self._locator_name = _create_locator()
            if self._locator_name is not None:
                self._publish_bbox_to_locator()
                self._publish_proxor_to_locator()
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
        # Ctrl/Alt change how the point is resolved, so recompute when they
        # toggle even if the cursor itself hasn't moved.
        place_mods = (ctrl_held, alt_held)
        last_mods = getattr(self, "_last_place_mods", None)
        if inside and (cur_px != last_px or place_mods != last_mods):
            # Qt reports cursor coords in logical pixels, but M3dView's
            # viewToWorld()/portHeight() operate in physical/device pixels.
            # On Retina/HiDPI displays (devicePixelRatio > 1, common on macOS)
            # the two differ, so the placement helper would otherwise drift
            # away from the cursor.  Scale logical → device pixels here.
            try:
                dpr = vp.devicePixelRatioF()
            except Exception:
                dpr = 1.0
            dev_x = round(local.x() * dpr)
            dev_y = round(local.y() * dpr)
            if alt_held:
                # Alt → drop to the floor, ignoring all geometry.
                loc = _project_cursor_to_floor(dev_x, dev_y)
                normal = (0.0, 1.0, 0.0)
                on_floor = True
                has_hit = True
            else:
                has_hit, loc, normal, on_floor, _hit_node = _raycast_scene(dev_x, dev_y)
                if ctrl_held:
                    # Ctrl → keep the hit position but stand the asset upright
                    # (ignore the surface normal).
                    normal = (0.0, 1.0, 0.0)
            if (
                loc != _active_state.location
                or has_hit != _active_state.has_hit
                or on_floor != _active_state.hit_floor
                or normal != _active_state.surface_normal
            ):
                _active_state.location = loc
                _active_state.has_hit = has_hit
                _active_state.hit_floor = on_floor
                _active_state.surface_normal = normal
                position_changed = True
        self._last_cursor_px = cur_px
        self._last_place_mods = place_mods

        # ── 4. Push only what changed to the locator node ─────────────────
        if not (position_changed or rotation_changed):
            return

        try:
            import maya.cmds as cmds

            if position_changed:
                loc = _active_state.location
                nrm = _active_state.surface_normal
                cmds.setAttr(self._locator_name + ".location", loc[0], loc[1], loc[2], type="double3")
                cmds.setAttr(self._locator_name + ".hasHit", bool(_active_state.has_hit))
                cmds.setAttr(self._locator_name + ".hitFloor", bool(_active_state.hit_floor))
                try:
                    cmds.setAttr(self._locator_name + ".surfaceNormal", nrm[0], nrm[1], nrm[2], type="double3")
                except Exception:
                    pass
            if rotation_changed:
                cmds.setAttr(self._locator_name + ".rotationY", _active_state.rotation_y)
        except Exception as exc:
            if self._tick_count <= 3:
                log.warning("Failed to push locator state: %s", exc)

        # ── 5. One async, non-blocking refresh per tick ───────────────────
        # M3dView.refresh(all=False, force=False) schedules a repaint of
        # the active view on Maya's next idle, instead of synchronously
        # repainting (which is what cmds.refresh(force=True) does and what
        # was previously starving the polling timer).
        _refresh_viewport(light=True)

    def _poll_material(self) -> None:
        """Cursor poll for material drops — pick the mesh under the cursor.

        Materials have no bounding-box helper; instead we raycast each tick
        and remember the mesh under the cursor as the assignment target.
        Only real geometry counts (the Y=0 floor fallback is ignored), so a
        material can only be dropped directly onto a mesh.
        """
        vp = self._vp_widget if _widget_alive(self._vp_widget) else None
        if vp is None:
            vp = _get_viewport_widget()
            self._vp_widget = vp
        if vp is None:
            return

        try:
            gp = QCursor.pos()
        except Exception:
            log.exception("Could not get global cursor position; stopping drag")
            return
        # Keep the cursor badge glued to the pointer, even outside the viewport.
        if self._overlay is not None:
            try:
                self._overlay.move_to(gp)
                self._overlay.raise_()
            except Exception:
                pass
        local = vp.mapFromGlobal(gp)
        inside = vp.rect().contains(local)

        if not inside:
            if _active_state.target_mesh or _active_state.has_hit:
                _active_state.target_mesh = ""
                _active_state.has_hit = False
                self._highlight_mesh("")
                self._set_material_hint(None)
            if self._overlay is not None:
                self._overlay.set_valid(False)
            return

        cur_px = (local.x(), local.y())
        if cur_px == getattr(self, "_last_cursor_px", None):
            return
        self._last_cursor_px = cur_px

        try:
            dpr = vp.devicePixelRatioF()
        except Exception:
            dpr = 1.0
        _hit, loc, normal, on_floor, hit_node = _raycast_scene(round(local.x() * dpr), round(local.y() * dpr))
        # Only a real mesh hit (not the floor fallback) is a valid target.
        mesh_hit = bool(hit_node) and not on_floor
        target = hit_node if mesh_hit else ""
        if self._overlay is not None:
            self._overlay.set_valid(mesh_hit)
        if target != _active_state.target_mesh:
            _active_state.target_mesh = target
            _active_state.has_hit = mesh_hit
            _active_state.location = loc
            _active_state.surface_normal = normal
            self._highlight_mesh(target)
            self._set_material_hint(target or None)

    def _poll_hdri(self) -> None:
        """Cursor poll for HDRI drops — just keep the badge under the cursor.

        HDRIs become a world-level environment light, so there is no mesh
        target and no raycast: the drop is valid anywhere inside the viewport.
        """
        vp = self._vp_widget if _widget_alive(self._vp_widget) else None
        if vp is None:
            vp = _get_viewport_widget()
            self._vp_widget = vp
        if vp is None:
            return
        try:
            gp = QCursor.pos()
        except Exception:
            log.exception("Could not get global cursor position; stopping drag")
            return
        if self._overlay is not None:
            try:
                self._overlay.move_to(gp)
                self._overlay.raise_()
            except Exception:
                pass
        local = vp.mapFromGlobal(gp)
        inside = vp.rect().contains(local)
        _active_state.has_hit = inside
        if self._overlay is not None:
            self._overlay.set_valid(inside)

    def _highlight_mesh(self, mesh: str) -> None:
        """Select the mesh under the cursor so it lights up in the viewport.

        This is the drag feedback for material drops (there is no bounding-box
        helper). Passing an empty string restores the user's original
        selection. The standard Maya selection highlight gives an unmistakable
        “this mesh will receive the material” cue.
        """
        if mesh == self._hilited_mesh:
            return
        self._hilited_mesh = mesh
        try:
            import maya.cmds as cmds

            if mesh and cmds.objExists(mesh):
                cmds.select(mesh, replace=True)
            elif self._orig_selection:
                existing = [n for n in self._orig_selection if cmds.objExists(n)]
                if existing:
                    cmds.select(existing, replace=True)
                else:
                    cmds.select(clear=True)
            else:
                cmds.select(clear=True)
        except Exception as exc:
            log.debug("material highlight select failed: %s", exc)
        _refresh_viewport(light=True)

    def _restore_selection(self) -> None:
        """Restore the selection captured at the start of a material drag."""
        self._hilited_mesh = ""
        try:
            import maya.cmds as cmds

            existing = [n for n in self._orig_selection if cmds.objExists(n)]
            if existing:
                cmds.select(existing, replace=True)
            else:
                cmds.select(clear=True)
        except Exception as exc:
            log.debug("material restore selection failed: %s", exc)
        self._orig_selection = []

    def _set_material_hint(self, mesh: str | None) -> None:
        """Show the current material-assignment target in the viewport HUD."""
        try:
            import maya.cmds as cmds
        except Exception:
            return
        if mesh:
            short = mesh.rsplit("|", 1)[-1]
            msg = f"Assign material to <hl>{short}</hl>"
        else:
            msg = "Hover a mesh to assign the material"
        try:
            cmds.inViewMessage(amg=msg, pos="topCenter", fade=False, clear="topCenter", fontSize=14)
        except Exception as exc:
            log.debug("material hint inViewMessage failed: %s", exc)

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
        """Register cached proxor data for the locator's draw override."""
        if self._locator_name is None:
            return
        try:
            from bk_maya.core import locator_state as plc

            plc.set_proxor_lines(self._locator_name, list(_active_state.proxor_lines or []))
            plc.set_proxor_mesh(self._locator_name, list(_active_state.proxor_mesh or []))
            # Seed the label with the asset name; status fills in once
            # the download controller starts firing progress events.
            asset_name = ""
            try:
                asset_name = str(_active_state.asset_data.get("name") or "")
            except Exception:
                pass
            plc.set_label(self._locator_name, name=asset_name, status="Ready to drop")
            # Mirror to viewport HUD so the user always sees current step,
            # regardless of camera framing or draw-override font availability.
            try:
                import maya.cmds as cmds

                msg = f"<hl>{asset_name}</hl><br>Ready to drop" if asset_name else "Ready to drop"
                cmds.inViewMessage(amg=msg, pos="topCenter", fade=False, clear="topCenter", fontSize=14)
            except Exception:
                pass
            log.info(
                "[PROXOR] published %d polyline(s), %d mesh-vert(s) on %s",
                len(_active_state.proxor_lines or []),
                len(_active_state.proxor_mesh or []),
                self._locator_name,
            )
        except Exception as exc:
            log.debug("Could not publish proxor data: %s", exc)

    def _start_proxor_fetch(self, asset_data: dict[str, Any]) -> None:
        """Kick off an async ``.prxc`` download via the local Go client.

        The HTTP work (``ensure_running`` + POST) runs on a daemon thread
        — ``ensure_running`` can block up to 8 s spawning the client and
        the POST itself can take seconds, and Maya's main thread MUST NOT
        block during a drag (the viewport freezes and no bbox is drawn).
        The completion callback is delivered later by the QTimer-driven
        ``/report`` poller, which is already on the main thread.
        """
        asset_type = asset_data.get("assetType")
        if asset_type not in ("model", "printable"):
            return
        url = _prxc_download_url(asset_data)
        if not url:
            return
        path = _proxor_cache_path(asset_data)
        if not path:
            return
        asset_base_id = asset_data.get("assetBaseId", "")
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except OSError as exc:
            log.debug("Could not create proxor cache dir: %s", exc)
            return

        try:
            from bk_maya.core import auth, client_lib
        except Exception as exc:
            log.debug("client_lib unavailable for proxor fetch: %s", exc)
            return

        # Register the callback now (main thread, cheap, lock-protected) so a
        # very fast /report delivery still finds a subscriber.
        try:
            client_lib.prxc_registry.register(asset_base_id, self._on_proxor_ready)
        except Exception as exc:
            log.debug("prxc_registry.register failed: %s", exc)
            return

        def _worker() -> None:
            try:
                client_lib.ensure_running()
                api_key = ""
                try:
                    api_key = auth.get_api_key() or ""
                except Exception:
                    pass
                task_id = client_lib.asset_prxc_download(
                    asset_base_id=asset_base_id,
                    download_url=url,
                    file_path=path,
                    api_key=api_key,
                )
                log.info("[PROXOR] download scheduled task=%s -> %s", task_id, path)
            except Exception as exc:
                log.debug("asset_prxc_download failed: %s", exc)
                try:
                    client_lib.prxc_registry.unregister(asset_base_id)
                except Exception:
                    pass

        import threading

        threading.Thread(
            target=_worker,
            name=f"bk-prxc-fetch-{asset_base_id[:8]}",
            daemon=True,
        ).start()

    def _on_proxor_ready(self, prxc_path: str) -> None:
        """Registry callback: ``.prxc`` is on disk — parse and republish."""
        if not _active_state.active:
            return  # drag already ended; nothing to do
        try:
            payload = _parse_prxc(prxc_path)
        except Exception as exc:
            log.debug("Proxor swap-in parse failed: %s", exc)
            return
        lines = payload.get("lines", [])
        mesh = payload.get("mesh", [])
        if not lines and not mesh:
            return
        _active_state.proxor_lines = lines
        _active_state.proxor_mesh = mesh
        self._publish_proxor_to_locator()
        _refresh_viewport(light=True)
        log.info("[PROXOR] live swap-in: %d segments, %d mesh-verts", len(lines), len(mesh))

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
        if vp is not None and gp is not None and not vp.rect().contains(vp.mapFromGlobal(gp)):
            return False

        step = -WHEEL_STEP if delta > 0 else WHEEL_STEP
        _active_state.rotation_y += step
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

        # Material mode: only assign when the cursor is over a mesh. A drop on
        # empty space / the floor does nothing (materials need a target mesh).
        if _active_state.is_material:
            if _active_state.has_hit and _active_state.target_mesh:
                # Leave the target mesh selected as feedback during the
                # download; the async assign step reselects it on completion.
                self._orig_selection = []
                self._trigger_download()
            else:
                log.info("Material dropped off-mesh — ignored (needs a mesh target).")
                self._restore_selection()
                try:
                    import maya.cmds as cmds

                    cmds.inViewMessage(
                        amg="Drop the material directly onto a mesh.",
                        pos="topCenter",
                        fade=True,
                        fadeStayTime=1500,
                    )
                except Exception:
                    pass
            self._cleanup()
            return

        # HDRI mode: drop anywhere in the viewport creates an environment
        # light. Location is irrelevant (world-level), so always download.
        if _active_state.is_hdri:
            if _active_state.has_hit:
                self._trigger_download()
            else:
                log.info("HDRI dropped outside the viewport — ignored.")
            self._cleanup()
            return

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
        loc = _active_state.location
        rot_y = _active_state.rotation_y
        asset = _active_state.asset_data
        log.info(
            "BK drop: '%s' at (%.3f, %.3f, %.3f) rot_y=%.1f°",
            asset.get("name", "?"),
            loc[0],
            loc[1],
            loc[2],
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
                surface_normal=_active_state.surface_normal,
                target_mesh=_active_state.target_mesh,
            )
        except ModuleNotFoundError:
            log.info(
                "download module not implemented yet - stub only. Asset '%s' would be placed at %s rot_y=%.1f°",
                asset.get("name", "?"),
                loc,
                rot_y,
            )
        except Exception as exc:
            log.error("Download dispatch failed: %s", exc)

    def _cleanup(self) -> None:
        global _active_state
        _active_state.active = False

        # Tear down the cursor-following drag badge (material drags).
        try:
            if self._overlay is not None:
                self._overlay.hide()
                self._overlay.deleteLater()
        except Exception:
            pass
        self._overlay = None

        # Material drag: restore the user's original selection if it wasn't
        # already consumed by a successful on-mesh drop (which clears
        # ``_orig_selection`` and keeps the target selected). On cancel
        # (RMB/ESC) or an off-mesh drop, ``_orig_selection`` is still set.
        if self._orig_selection:
            self._restore_selection()
        self._hilited_mesh = ""

        # Drop any pending proxor-download subscription so a late /report
        # for this asset doesn't fire on a dead drag session.
        try:
            abid = (_active_state.asset_data or {}).get("assetBaseId", "")
            if abid:
                from bk_maya.core import client_lib

                client_lib.prxc_registry.unregister(abid)
        except Exception:
            pass

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
            cmds.inViewMessage(clear="topCenter")
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
    # Materials must be dropped onto a mesh — a click (no drag) has no target,
    # so there is nothing to place at the origin. Hint the user to drag instead.
    if _is_material_asset(asset_data):
        log.info("Material clicked (no drag) — drag it onto a mesh to assign it.")
        try:
            import maya.cmds as cmds

            cmds.inViewMessage(
                amg="Drag the material onto a mesh to assign it.",
                pos="topCenter",
                fade=True,
                fadeStayTime=1500,
            )
        except Exception:
            pass
        return
    # HDRIs are world-level — a click is enough to add the environment light
    # (no location needed). Kick the download straight off.
    if _is_hdri_asset(asset_data):
        log.info("HDRI clicked (no drag) — adding as environment light.")
        try:
            import importlib

            bk_dl = importlib.import_module("bk_maya.core.download")
            bk_dl.download_asset(asset_data)
        except Exception as exc:
            log.error("HDRI download dispatch failed: %s", exc)
        return
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

    default_min = (-0.5 * scale, 0.0, -0.5 * scale)
    default_max = (0.5 * scale, 1.0 * scale, 0.5 * scale)
    bbox_min = _coerce_bbox(asset_data.get("bbox_min"), default_min)
    bbox_max = _coerce_bbox(asset_data.get("bbox_max"), default_max)
    sw_min = (bbox_min[0], bbox_min[2], bbox_min[1])
    sw_max = (bbox_max[0], bbox_max[2], bbox_max[1])
    bbox_min = tuple(min(sw_min[i], sw_max[i]) for i in range(3))
    bbox_max = tuple(max(sw_min[i], sw_max[i]) for i in range(3))
    _prxc = _load_proxor_payload(asset_data)
    _active_state = _State(
        asset_data=asset_data,
        thumb_path=thumb_path,
        bbox_min=bbox_min,  # type: ignore
        bbox_max=bbox_max,  # type: ignore
        location=(0.0, 0.0, 0.0),
        rotation_y=0.0,
        active=True,
        proxor_lines=_prxc.get("lines", []),
        proxor_mesh=_prxc.get("mesh", []),
    )
    log.info("Place at origin: asset=%s bbox_min=%s bbox_max=%s", asset_data.get("name", "?"), bbox_min, bbox_max)
    # Create locator and hand ownership to the download controller via
    # DragSession.  Without registering the locator on the session, the
    # download module would receive an empty locator_name and could
    # neither update the label/progress nor delete it when finished —
    # leaving a stuck red bounding box at the origin.
    locator = _create_locator()
    session = DragSession.get()
    session._locator_name = locator
    session._download_started = True  # tell _cleanup not to delete it
    try:
        import maya.cmds as cmds

        if locator and cmds.objExists(locator):
            cmds.setAttr(locator + ".location", 0.0, 0.0, 0.0, type="float3")
            cmds.setAttr(locator + ".hasHit", True)
            try:
                cmds.setAttr(locator + ".downloadState", 1)  # downloading
            except Exception:
                pass
    except Exception as exc:
        log.debug("place_at_origin: locator attr setup failed: %s", exc)

    # Publish bbox + cached proxor so the locator draw override has data
    # immediately. If the .prxc isn't on disk yet, fetch it asynchronously
    # — the registry callback will swap it in mid-download (same as drag).
    try:
        session._publish_bbox_to_locator()
        session._publish_proxor_to_locator()
    except Exception:
        pass
    if not _active_state.proxor_lines and not _active_state.proxor_mesh:
        try:
            _start_proxor_fetch_for_locator(asset_data, locator)  # type: ignore
        except Exception as exc:
            log.debug("place_at_origin: proxor fetch failed: %s", exc)

    session._trigger_download()
    # Release the session so a subsequent click can start a new placement.
    # The locator is now owned by the download controller, and the proxor
    # swap-in is wired through the locator-bound callback registered above
    # (independent of session state).
    _active_state.active = False
    session._locator_name = None
    session._download_started = False
