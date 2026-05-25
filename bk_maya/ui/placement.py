"""BlenderKit Maya – drag-to-place asset system.

Architecture
============
Drag starts from an AssetTile (inside the PySide panel) and places the
asset into the Maya 3D viewport.

::

    AssetTile.mouseMoveEvent
        └─ start_drag(asset_data, thumb_path)
               └─ DragSession.start()
                      ├─ QApplication.setOverrideCursor(thumb pixmap)
                      ├─ _ViewportOverlay(vp_widget)   ← transparent QPainter overlay
                      └─ QApplication.installEventFilter(self)
                             ├─ MouseMove  → ray-cast → update state → overlay.update()
                             ├─ Wheel      → rotate_y ± 15°
                             ├─ LMB up     → _trigger_download() + cleanup
                             ├─ RMB up     → cancel
                             └─ Escape     → cancel

Drawing
=======
*  Proxor wireframe: the ``line`` section of the .prxc payload is projected
   to screen and drawn as coloured line pairs.
*  Fallback bbox: 12 edges of the axis-aligned bounding box, green when valid
   (has geometry hit or floor hit) and red when no placement surface found.

Ray casting
===========
1. Try geometry hit: iterate visible MFnMesh nodes with closestIntersection.
2. Fallback: intersect the Y=0 floor plane.
"""
from __future__ import annotations

import logging
import math
import os
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger("bk_maya.placement")

# ---------------------------------------------------------------------------
# Qt imports (deferred so the module is importable outside Maya)
# ---------------------------------------------------------------------------
try:
    from qtpy.QtCore import QEvent, QObject, QPoint, Qt, QTimer
    from qtpy.QtGui import QColor, QCursor, QPainter, QPen, QPixmap
    from qtpy.QtWidgets import QApplication, QWidget
    _QT = True
except ImportError:  # pragma: no cover
    _QT = False

# How many pixels the mouse must travel before drag starts.
DRAG_THRESHOLD = 8

# Rotation step per wheel tick (degrees).
WHEEL_STEP = 15.0

# Bbox corner index order: 4 bottom + 4 top, winding matches the 12 edges below.
_BBOX_EDGES = [
    (0, 1), (1, 2), (2, 3), (3, 0),   # bottom face
    (4, 5), (5, 6), (6, 7), (7, 4),   # top face
    (0, 4), (1, 5), (2, 6), (3, 7),   # vertical pillars
]

# Green / red / orange palette
_COLOR_VALID   = QColor(0, 220, 80,  220) if _QT else None
_COLOR_INVALID = QColor(220, 50, 50, 220) if _QT else None
_COLOR_FLOOR   = QColor(100, 200, 255, 200) if _QT else None  # floor-plane hit

# ─────────────────────────────────────────────────────────────────────────────
# Placement state
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class _State:
    asset_data: dict[str, Any] = field(default_factory=dict)
    thumb_path: str  = ""
    # bbox in object-local space (same as BlenderKit API bbox_min/bbox_max)
    bbox_min:   tuple[float, float, float] = (-0.5, 0.0, -0.5)
    bbox_max:   tuple[float, float, float] = ( 0.5, 1.0,  0.5)
    # current world placement
    location:   tuple[float, float, float] = (0.0, 0.0, 0.0)
    rotation_y: float = 0.0   # degrees, controlled by mouse wheel
    # hit status
    has_hit:    bool  = False
    hit_floor:  bool  = False   # True when using floor fallback
    active:     bool  = False
    # proxor line data (may be empty list)
    proxor_lines: list[list[tuple[float, float, float]]] = field(default_factory=list)


# ─────────────────────────────────────────────────────────────────────────────
# Math helpers
# ─────────────────────────────────────────────────────────────────────────────

def _rotate_y(pt: tuple, deg: float) -> tuple:
    """Rotate point (x, y, z) around Y axis by *deg* degrees."""
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    x, y, z = pt
    return (x * c - z * s, y, x * s + z * c)


def _bbox_corners(loc: tuple, rot_y: float,
                  mn: tuple, mx: tuple) -> list[tuple]:
    """Return 8 world-space corners of the rotated bbox."""
    local = [
        (mn[0], mn[1], mn[2]), (mx[0], mn[1], mn[2]),
        (mx[0], mn[1], mx[2]), (mn[0], mn[1], mx[2]),
        (mn[0], mx[1], mn[2]), (mx[0], mx[1], mn[2]),
        (mx[0], mx[1], mx[2]), (mn[0], mx[1], mx[2]),
    ]
    cx, cy, cz = loc
    out = []
    for lx, ly, lz in local:
        rx, ry, rz = _rotate_y((lx, ly, lz), rot_y)
        out.append((cx + rx, cy + ry, cz + rz))
    return out


def _transform_proxor_lines(
    lines: list[list[tuple]],
    loc: tuple,
    rot_y: float,
) -> list[tuple[tuple, tuple]]:
    """Apply location + rot_y transform to all proxor line segments.

    Returns a flat list of (ptA, ptB) world-space pairs.
    """
    cx, cy, cz = loc
    result = []
    for polyline in lines:
        for i in range(len(polyline) - 1):
            ax, ay, az = _rotate_y(polyline[i], rot_y)
            bx, by, bz = _rotate_y(polyline[i + 1], rot_y)
            result.append(
                ((cx + ax, cy + ay, cz + az),
                 (cx + bx, cy + by, cz + bz))
            )
    return result


# ─────────────────────────────────────────────────────────────────────────────
# Maya viewport helpers (safe – all imports in try/except)
# ─────────────────────────────────────────────────────────────────────────────

def _get_viewport_widget() -> "QWidget | None":
    """Return the QWidget for Maya's active 3D viewport.

    Tries four strategies in order:
    1. M3dView.widget() – already a QWidget (Maya 2025+ / PySide6)
    2. M3dView.widget() wrapped via shiboken
    3. MQtUtil.findControl(modelEditor) wrapped via shiboken
    4. Qt widget hierarchy scan as last resort
    """
    # ── Strategy 1 & 2: M3dView.widget() ─────────────────────────────────
    try:
        import maya.OpenMayaUI as omui1
        view = omui1.M3dView.active3dView()
        ptr = view.widget()

        # Maya 2025+ may already return a QWidget
        if isinstance(ptr, QWidget):
            log.debug("BK viewport: M3dView.widget() is QWidget directly")
            return ptr

        if ptr is not None and int(ptr) != 0:
            for sh_name in ("shiboken6", "shiboken2"):
                try:
                    sh = __import__(sh_name)
                    w = sh.wrapInstance(int(ptr), QWidget)
                    log.debug("BK viewport: wrapped via %s", sh_name)
                    return w
                except Exception as _e:
                    log.debug("BK viewport: %s failed: %s", sh_name, _e)
            try:
                import sip
                w = sip.wrapinstance(int(ptr), QWidget)
                log.debug("BK viewport: wrapped via sip")
                return w
            except Exception:
                pass
    except Exception as e:
        log.debug("BK viewport: M3dView.widget() error: %s", e)

    # ── Strategy 3: MQtUtil.findControl on the active model panel ─────────
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
                            w = sh.wrapInstance(int(ptr), QWidget)
                            log.debug("BK viewport: MQtUtil found %s via %s", panel, sh_name)
                            return w
                        except Exception:
                            pass
            except Exception:
                pass
    except Exception as e:
        log.debug("BK viewport: MQtUtil fallback error: %s", e)

    # ── Strategy 4: Qt widget hierarchy scan ─────────────────────────────
    best: "QWidget | None" = None
    best_area = 0
    for w in QApplication.allWidgets():
        if not w.isVisible():
            continue
        cn = w.metaObject().className() if w.metaObject() else ""
        if any(kw in cn for kw in (
            "GLWidget", "MayaGL", "THoverQ", "modelEditor",
            "MayaViewport", "ViewportUI",
        )):
            area = w.width() * w.height()
            if area > best_area:
                best, best_area = w, area
    if best:
        log.debug("BK viewport: Qt scan found %s", best.metaObject().className())
    else:
        log.warning("BK viewport: could not find 3D viewport widget")
    return best


def _world_to_screen(pt3d: tuple) -> "tuple[int, int] | None":
    """Project a 3D world point to 2D Qt-space screen coordinates.

    Uses Maya Python API 2.0 – worldToView returns (x, y, inFront) directly.
    Returns None when the point is behind the camera.
    """
    try:
        import maya.api.OpenMaya as om2
        import maya.api.OpenMayaUI as omui2
        view = omui2.M3dView.active3dView()
        world_pt = om2.MPoint(float(pt3d[0]), float(pt3d[1]), float(pt3d[2]))
        # API 2.0: worldToView returns (unsigned int x, unsigned int y, bool inFront)
        sx, sy, in_front = view.worldToView(world_pt)
        vp_h = view.portHeight()
        return int(sx), int(vp_h - sy)   # flip y: OpenGL bottom→Qt top
    except Exception as e:
        log.debug("worldToView failed: %s", e)
        return None


def _raycast_scene(vp_x: int, vp_y: int) -> "tuple[bool, tuple, bool]":
    """Cast a ray from viewport pixel (vp_x, vp_y) and find the hit position.

    Uses Maya Python API 2.0 throughout.
    Returns (has_hit, (x,y,z), hit_floor) where *hit_floor* is True when
    the hit is on the Y=0 fallback plane (no scene geometry was struck).
    """
    try:
        import maya.api.OpenMaya as om2
        import maya.api.OpenMayaUI as omui2

        view = omui2.M3dView.active3dView()
        vp_h = view.portHeight()
        maya_y = vp_h - vp_y   # flip Y: Qt top→Maya bottom convention

        # World-space ray from near to far clip plane
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

        # ── Geometry intersection (API 2.0) ────────────────────────────────
        closest_dist = float("inf")
        best_hit: "tuple | None" = None

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
                    hit_pt = result[0]   # MFloatPoint
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

        # ── Floor plane fallback (Y = 0) ──────────────────────────────────
        ox, oy, oz = near_pt.x, near_pt.y, near_pt.z
        dx = far_pt.x - near_pt.x
        dy = far_pt.y - near_pt.y
        dz = far_pt.z - near_pt.z
        if abs(dy) > 1e-9:
            t = -oy / dy
            if t > 0:
                return True, (ox + t*dx, 0.0, oz + t*dz), True

    except Exception as exc:
        log.debug("Raycast error: %s", exc)

    return False, (0.0, 0.0, 0.0), False


# ─────────────────────────────────────────────────────────────────────────────
# Viewport overlay widget
# ─────────────────────────────────────────────────────────────────────────────

class _ViewportOverlay(QWidget):  # type: ignore[misc]
    """Frameless transparent top-level window drawn over the Maya 3D viewport.

    Must be top-level (not a child widget) for WA_TranslucentBackground to
    work on Windows when the viewport is a native OpenGL context.
    """

    def __init__(self, vp_widget: "QWidget", session: "DragSession") -> None:
        # Top-level, NOT a child of the viewport.
        super().__init__(None)
        self._session = session
        self._vp = vp_widget

        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.Tool
            | Qt.WindowStaysOnTopHint
            | Qt.WindowTransparentForInput
            | Qt.NoDropShadowWindowHint,
        )
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_NoSystemBackground)

        self._refit()
        self.show()

        # Track viewport resize / move so we stay aligned
        vp_widget.installEventFilter(self)

    def _refit(self) -> None:
        """Reposition/resize to exactly cover the viewport widget."""
        global_origin = self._vp.mapToGlobal(QPoint(0, 0))
        self.setGeometry(
            global_origin.x(), global_origin.y(),
            self._vp.width(), self._vp.height(),
        )

    # ── QObject interface ────────────────────────────────────────────────

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self._vp and event.type() in (QEvent.Resize, QEvent.Move):
            self._refit()
            self.update()
        return False

    # ── Drawing ──────────────────────────────────────────────────────────

    def paintEvent(self, _event) -> None:  # type: ignore[override]
        state = self._session.state
        if not state.active:
            return

        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)

        # Must explicitly erase to transparent on Windows top-level windows.
        painter.setCompositionMode(QPainter.CompositionMode_Clear)
        painter.fillRect(self.rect(), Qt.transparent)
        painter.setCompositionMode(QPainter.CompositionMode_SourceOver)

        if state.has_hit:
            color = _COLOR_FLOOR if state.hit_floor else _COLOR_VALID
        else:
            color = _COLOR_INVALID

        pen = QPen(color, 2, Qt.SolidLine)
        painter.setPen(pen)

        if state.proxor_lines and state.has_hit:
            self._draw_proxor(painter, state)
        else:
            self._draw_bbox(painter, state)

        # Status label (bottom of overlay)
        if state.has_hit:
            status = (
                "Floor plane" if state.hit_floor
                else f"{state.asset_data.get('name', 'Asset')}"
            )
            hint = f"{status}  |  Wheel: rotate  |  LMB: place  |  RMB / ESC: cancel"
        else:
            hint = "No surface — move over scene geometry or floor  |  ESC: cancel"

        painter.setPen(QPen(color, 1))
        painter.drawText(
            self.rect().adjusted(8, 0, -8, -8),
            Qt.AlignBottom | Qt.AlignHCenter,
            hint,
        )

        painter.end()

    def _draw_bbox(
        self,
        painter: QPainter,
        state: _State,
    ) -> None:
        corners = _bbox_corners(state.location, state.rotation_y,
                                state.bbox_min, state.bbox_max)
        screen = [_world_to_screen(c) for c in corners]
        for ai, bi in _BBOX_EDGES:
            sa, sb = screen[ai], screen[bi]
            if sa and sb:
                painter.drawLine(sa[0], sa[1], sb[0], sb[1])

    def _draw_proxor(
        self,
        painter: QPainter,
        state: _State,
    ) -> None:
        segments = _transform_proxor_lines(
            state.proxor_lines, state.location, state.rotation_y
        )
        pen_saved = painter.pen()
        for (ax, ay, az), (bx, by, bz) in segments:
            sa = _world_to_screen((ax, ay, az))
            sb = _world_to_screen((bx, by, bz))
            if sa and sb:
                painter.drawLine(sa[0], sa[1], sb[0], sb[1])
        painter.setPen(pen_saved)


# ─────────────────────────────────────────────────────────────────────────────
# Proxor loader
# ─────────────────────────────────────────────────────────────────────────────

def _load_proxor_lines(asset_data: dict[str, Any]) -> list[list[tuple]]:
    """Try to load the proxor wireframe for *asset_data* from local cache.

    Returns a list of polylines (each a list of (x,y,z) tuples).  Returns []
    if no .prxc is available, without raising.
    """
    asset_base_id = asset_data.get("assetBaseId", "")
    if not asset_base_id:
        return []

    # Locate cached .prxc
    try:
        import bk_maya.core.paths as bk_paths
        prxc_path = os.path.join(
            bk_paths.get_cache_dir(), "proxors", f"{asset_base_id}.prxc"
        )
        if not os.path.isfile(prxc_path):
            return []
    except Exception:
        return []

    try:
        # Import the proxor format reader bundled with the Blender addon.
        # It lives in <workspace>/bl_proxor/ and must be importable.
        from bl_proxor import prx_format as pf
        payload = pf.read_prx(prxc_path)
        data    = payload.get("data", {})
        line_sec = data.get("line", {})
        positions = line_sec.get("pos", [])

        # positions is a flat list of [x,y,z] pairs for each line segment.
        # Group them into polylines by pairs (each pair is one segment).
        lines = []
        for i in range(0, len(positions) - 1, 2):
            a = tuple(float(v) for v in positions[i][:3])
            b = tuple(float(v) for v in positions[i + 1][:3])
            lines.append([a, b])
        return lines
    except Exception as exc:
        log.debug("Proxor load failed for %s: %s", asset_base_id, exc)
        return []


# ─────────────────────────────────────────────────────────────────────────────
# Thumbnail cursor
# ─────────────────────────────────────────────────────────────────────────────

_CURSOR_SIZE = 64


def _make_cursor(thumb_path: str) -> "QCursor":
    """Return a QCursor using the asset thumbnail."""
    pix = QPixmap(thumb_path) if thumb_path and os.path.isfile(thumb_path) else QPixmap()
    if pix.isNull():
        # Fallback: a simple crosshair
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
    return QCursor(pix, 0, 0)   # hotspot at top-left corner of thumbnail


# ─────────────────────────────────────────────────────────────────────────────
# Drag session  (singleton event filter)
# ─────────────────────────────────────────────────────────────────────────────

class DragSession(QObject):
    """Global Qt event filter that drives a single drag-to-place operation.

    Use :func:`start_drag` to start a session; the class is a singleton.
    """

    _instance: "DragSession | None" = None

    @classmethod
    def get(cls) -> "DragSession":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def __init__(self) -> None:
        super().__init__()
        self.state    = _State()
        self._overlay: "_ViewportOverlay | None" = None
        self._vp_widget: "QWidget | None" = None

    # ── Public API ────────────────────────────────────────────────────────

    def start(self, asset_data: dict[str, Any], thumb_path: str) -> None:
        if self.state.active:
            log.debug("DragSession already active; ignoring start()")
            return

        bbox_min = tuple(asset_data.get("bbox_min", (-0.5, 0.0, -0.5)))[:3]
        bbox_max = tuple(asset_data.get("bbox_max", ( 0.5, 1.0,  0.5)))[:3]

        self.state = _State(
            asset_data   = asset_data,
            thumb_path   = thumb_path,
            bbox_min     = bbox_min,
            bbox_max     = bbox_max,
            active       = True,
            proxor_lines = _load_proxor_lines(asset_data),
        )

        # Override cursor with thumbnail
        QApplication.setOverrideCursor(_make_cursor(thumb_path))

        # Create viewport overlay
        vp = _get_viewport_widget()
        self._vp_widget = vp
        if vp is not None:
            self._overlay = _ViewportOverlay(vp, self)
        else:
            log.warning("BK drag: could not find Maya 3D viewport widget")

        # Capture all Qt events
        QApplication.instance().installEventFilter(self)
        log.debug(
            "DragSession started: asset=%s proxor_segments=%d",
            asset_data.get("name", "?"),
            len(self.state.proxor_lines),
        )

    def cancel(self) -> None:
        log.debug("DragSession cancelled")
        self._cleanup()

    # ── QObject.eventFilter ───────────────────────────────────────────────

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:  # type: ignore[override]
        if not self.state.active:
            return False

        et = event.type()

        if et == QEvent.MouseMove:
            self._on_mouse_move(event)
            return False   # don't eat moves – Maya needs them for navigation

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

    def _on_mouse_move(self, event: QEvent) -> None:
        vp = self._vp_widget or _get_viewport_widget()
        if vp is None:
            return

        # Refresh overlay widget reference if viewport changed
        if vp is not self._vp_widget:
            self._vp_widget = vp
            if self._overlay:
                self._overlay.deleteLater()
            self._overlay = _ViewportOverlay(vp, self)

        local = vp.mapFromGlobal(event.globalPos())

        # Only ray-cast when cursor is actually inside the viewport
        if vp.rect().contains(local):
            has_hit, loc, on_floor = _raycast_scene(local.x(), local.y())
            self.state.has_hit  = has_hit
            self.state.hit_floor = on_floor
            if has_hit:
                self.state.location = loc

        if self._overlay:
            self._overlay.update()

    def _on_wheel(self, event: QEvent) -> bool:
        """Rotate placement by WHEEL_STEP degrees per tick."""
        vp = self._vp_widget
        if vp is None:
            return False
        # Only intercept wheel when the cursor is over the viewport
        local = vp.mapFromGlobal(event.globalPos())
        if not vp.rect().contains(local):
            return False
        try:
            delta = event.angleDelta().y()
        except AttributeError:
            delta = event.delta()
        self.state.rotation_y += WHEEL_STEP if delta > 0 else -WHEEL_STEP
        if self._overlay:
            self._overlay.update()
        return True   # consumed

    def _on_drop(self) -> None:
        if self.state.has_hit:
            self._trigger_download()
        self._cleanup()

    def _trigger_download(self) -> None:
        """Dispatch asset download at current placement location and rotation."""
        state = self.state
        loc   = state.location
        rot_y = state.rotation_y
        asset = state.asset_data
        log.info(
            "BK drop: '%s' at (%.3f, %.3f, %.3f) rot_y=%.1f°",
            asset.get("name", "?"),
            loc[0], loc[1], loc[2],
            rot_y,
        )
        try:
            from bk_maya.core import download as bk_dl
            bk_dl.download_asset(asset, location=loc, rotation_y=rot_y)
        except Exception as exc:
            log.error("Download dispatch failed: %s", exc)

    def _cleanup(self) -> None:
        self.state.active = False
        try:
            QApplication.instance().removeEventFilter(self)
        except Exception:
            pass
        try:
            QApplication.restoreOverrideCursor()
        except Exception:
            pass
        if self._overlay is not None:
            self._overlay.deleteLater()
            self._overlay = None
        self._vp_widget = None
        log.debug("DragSession cleaned up")


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def start_drag(asset_data: dict[str, Any], thumb_path: str) -> None:
    """Begin a drag-to-place session.

    Called from :class:`~bk_maya.ui.asset_bar.AssetTile` once the drag
    threshold has been reached.
    """
    if not _QT:
        log.warning("Qt not available – cannot start drag session")
        return
    DragSession.get().start(asset_data, thumb_path)
