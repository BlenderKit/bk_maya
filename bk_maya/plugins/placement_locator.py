"""Blendkit placement locator - Maya viewport draw override.

A custom `MPxLocatorNode` whose `MPxDrawOverride` renders the drag-to-place
visualisation directly with `MUIDrawManager`.

Visual elements
===============
*  Bounding-box edges (12 lines) - green when valid (geometry hit), cyan when
   falling back to the Y=0 floor, red when no surface.
*  Optional proxor wireframe - yellow line segments transformed by location +
   Y-rotation.
*  Screen-space hint text in the bottom centre (Wheel: rotate, etc.).

The locator is a *transient* node: created lazily when the drag session starts
and deleted on completion / cancellation.  It carries no data of its own -
:func:`bk_maya.ui.placement.get_drag_snapshot` is queried each draw cycle.
"""

from __future__ import annotations

import logging
import math
import os
import sys

import maya.api.OpenMaya as om2
import maya.api.OpenMayaRender as omr2
import maya.api.OpenMayaUI as omui2

log = logging.getLogger("bk_maya.placement_locator")


# ---------------------------------------------------------------------------
# Shared mutable state
# ---------------------------------------------------------------------------
# Maya's ``cmds.loadPlugin`` loads this file as a *plug-in*, which gives it a
# module identity separate from anything the addon imports through the usual
# Python import system.  To avoid two unrelated dicts (one written by the
# addon, one read by the draw override) we route all per-locator scratch state
# through ``bk_maya.core.locator_state`` instead of holding it in this file's
# own globals.
#
# We bootstrap sys.path so this works even when Maya loaded the plug-in by
# absolute path (which leaves ``bk_maya`` unreachable in the plug-in's
# private module namespace).  Maya's ``loadPlugin`` does NOT inject
# ``__file__`` into the plug-in's globals, so we have to find this file's
# location another way.
def _resolve_plugin_dir() -> str:
    f = globals().get("__file__")
    if f:
        return os.path.dirname(os.path.abspath(f))
    # Fallback: search sys.path for a matching module file.
    for d in sys.path:
        cand = os.path.join(d, "bk_maya", "plugins", "placement_locator.py")
        if os.path.isfile(cand):
            return os.path.dirname(cand)
    return ""


_here = _resolve_plugin_dir()  # …/bk_maya/plugins
_bk_root = os.path.dirname(os.path.dirname(_here)) if _here else ""  # repo root
if _bk_root and _bk_root not in sys.path:
    sys.path.insert(0, _bk_root)

from bk_maya.core import locator_state as _state


def set_proxor_lines(node_name: str, lines: list) -> None:
    """Register proxor polylines for *node_name* (use ``[]`` to clear)."""
    if lines:
        _state.set_proxor_lines(node_name, lines)
    else:
        _state.clear_proxor_lines(node_name)


def clear_proxor_lines(node_name: str) -> None:
    """Remove any proxor data registered for *node_name*."""
    _state.clear_proxor_lines(node_name)


def set_proxor_mesh(node_name: str, verts: list) -> None:
    """Register a flat list of triangle vertices for *node_name*.

    *verts* must already be in Maya local space (axis-swapped, scaled).
    Pass ``[]`` to clear.
    """
    if verts:
        _state.set_proxor_mesh(node_name, verts)
    else:
        _state.clear_proxor_mesh(node_name)


def clear_proxor_mesh(node_name: str) -> None:
    _state.clear_proxor_mesh(node_name)


def set_label(node_name: str, name: str | None = None, status: str | None = None) -> None:
    """Update the on-screen labels for *node_name*."""
    _state.set_label(node_name, name=name, status=status)


def clear_label(node_name: str) -> None:
    """Remove any label data registered for *node_name*."""
    _state.clear_label(node_name)


# ---------------------------------------------------------------------------
# Maya registration constants
# ---------------------------------------------------------------------------

NODE_NAME = "bkPlacementLocator"
# Arbitrary node-id in the "developer-experimental" range (0x00120000-0x0012FFFF
# is normally safe; use 0x0013FA60 to avoid collisions).
NODE_ID = om2.MTypeId(0x0013FA60)
DRAW_CLASSIFICATION = "drawdb/geometry/bkPlacementLocator"
DRAW_REGISTRANT_ID = "BkPlacementLocatorPlugin"


def maya_useNewAPI() -> None:
    """Marker: this module uses Maya Python API 2.0."""


# ---------------------------------------------------------------------------
# Bounding-box helpers (duplicated here so the locator has no UI dependency)
# ---------------------------------------------------------------------------

_BBOX_EDGES = [
    (0, 1),
    (1, 2),
    (2, 3),
    (3, 0),
    (4, 5),
    (5, 6),
    (6, 7),
    (7, 4),
    (0, 4),
    (1, 5),
    (2, 6),
    (3, 7),
]

# Edges for the orientation arrow: triangle at the front bottom edge (corners 2,3 are +Z).
# Index 8 is the tip, 2 and 3 are the front-bottom corners.
_ARROW_EDGES = [(2, 8), (3, 8), (2, 3)]


def _rotate_y(pt: tuple, deg: float) -> tuple:
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    x, y, z = pt
    return (x * c - z * s, y, x * s + z * c)


def _align_y_to_normal(pt: tuple, normal: tuple) -> tuple:
    """Rodrigues rotation that maps local (0,1,0) onto *normal*.

    Lets the asset sit "stuck" on the surface like a sticker. Composes
    AFTER an in-plane ``_rotate_y`` spin so the wheel rotation behaves
    intuitively as spin around the surface normal.
    """
    nx, ny, nz = normal
    # Axis = cross((0,1,0), n) = (nz, 0, -nx).
    ax, az = nz, -nx
    sin2 = ax * ax + az * az
    if sin2 < 1e-12:
        # Already aligned (n ~= +Y) or fully flipped (n ~= -Y).
        if ny >= 0.0:
            return pt
        x, y, z = pt
        return (x, -y, -z)
    sin_t = math.sqrt(sin2)
    cos_t = ny
    ax /= sin_t
    az /= sin_t
    x, y, z = pt
    # Rodrigues with k=(ax,0,az):
    #   p' = p*cos + (k×p)*sin + k*(k·p)*(1-cos)
    one_minus_c = 1.0 - cos_t
    cx = -az * y  # (k × p).x
    cy = az * x - ax * z  # (k × p).y
    cz = ax * y  # (k × p).z
    kdp = ax * x + az * z  # k · p
    rx = x * cos_t + cx * sin_t + ax * kdp * one_minus_c
    ry = y * cos_t + cy * sin_t  # k.y = 0
    rz = z * cos_t + cz * sin_t + az * kdp * one_minus_c
    return (rx, ry, rz)


def _local_to_world(pt: tuple, loc: tuple, rot_y: float, normal: tuple) -> tuple:
    """Local-space -> world: spin around local Y, align Y to *normal*, translate."""
    spun = _rotate_y(pt, rot_y)
    oriented = _align_y_to_normal(spun, normal)
    return (loc[0] + oriented[0], loc[1] + oriented[1], loc[2] + oriented[2])


# --- Proxor hologram-mesh draw ---------------------------------------------
# Mirrors the Blender addon's draw_proxor_download() look (mesh + vertical
# alpha gradient + progress reveal sweep), minus the glowing back-face outline.
#
# The PRX mesh section stores triangle vertices consecutively (3 verts per
# triangle, no index buffer).  We:
#  * transform each vert to world space (rotate around local-Y, align to
#    the surface normal, translate to *loc*);
#  * compute a per-vertex RGBA where alpha = hologram-gradient * reveal-band
#    using the vertex's *local-Y* (height above the ground), matching
#    bl_proxor.draw._apply_vertical_gradient();
#  * cull whole triangles fully above the reveal band so we don't pay for
#    invisible geometry.
_PROXOR_HOLO_RGB = (0.0, 1.0, 0.0)  # green - matches addon's draw_proxor_download default
_PROXOR_IDLE_RGB = (0.0, 1.0, 0.0)
_PROXOR_GRAD_LOW = 0.35
_PROXOR_GRAD_HIGH = 1.00
_PROXOR_BAND_WIDTH = 0.10
_PROXOR_ALPHA_TOP = 0.45


def _draw_proxor_mesh(
    drawManager,
    verts_local: list,
    loc: tuple,
    rot_y: float,
    normal: tuple,
    y_min: float,
    y_max: float,
    vis_pct_float: float,
    downloading: bool,
) -> None:
    """Draw the proxor mesh as translucent triangles with reveal sweep.

    *verts_local* is a flat list of ``(x, y, z)`` tuples, 3 consecutive
    verts = 1 triangle, in the locator's local space (Maya internal units).
    """
    n = len(verts_local) // 3
    if n <= 0:
        return

    y_range = max(1e-6, y_max - y_min)
    half_band = _PROXOR_BAND_WIDTH * 0.5
    # smoothstep band centre sweeps from below mesh (vis=0) to above (vis=1)
    band_centre = -half_band + vis_pct_float * (1.0 + _PROXOR_BAND_WIDTH)
    band_low = band_centre - half_band
    band_high = band_centre + half_band
    band_span = max(band_high - band_low, 1e-8)

    r, g, b = _PROXOR_HOLO_RGB if downloading else _PROXOR_IDLE_RGB

    pos_array = om2.MPointArray()
    col_array = om2.MColorArray()

    for tri in range(n):
        i0 = tri * 3
        v0 = verts_local[i0]
        v1 = verts_local[i0 + 1]
        v2 = verts_local[i0 + 2]
        # Quick triangle cull: if all 3 verts are above the reveal band,
        # the whole triangle is invisible -> skip.
        t0 = (v0[1] - y_min) / y_range
        t1 = (v1[1] - y_min) / y_range
        t2 = (v2[1] - y_min) / y_range
        if min(t0, t1, t2) >= band_high:
            continue

        for vert, t in ((v0, t0), (v1, t1), (v2, t2)):
            wx, wy, wz = _local_to_world(vert, loc, rot_y, normal)
            pos_array.append(om2.MPoint(wx, wy, wz))
            t_c = max(0.0, min(1.0, t))
            v_mult = _PROXOR_GRAD_LOW + (_PROXOR_GRAD_HIGH - _PROXOR_GRAD_LOW) * t_c
            hologram_a = 1.0 + (_PROXOR_ALPHA_TOP - 1.0) * t_c
            s = max(0.0, min(1.0, (t_c - band_low) / band_span))
            reveal_a = 1.0 - (s * s * (3.0 - 2.0 * s))  # smoothstep
            a = hologram_a * reveal_a
            col_array.append(om2.MColor((r * v_mult, g * v_mult, b * v_mult, a)))

    if len(pos_array) == 0:
        return

    try:
        drawManager.mesh(omr2.MUIDrawManager.kTriangles, pos_array, None, col_array)
    except Exception as exc:
        # Older MUIDrawManager bindings may use kTris instead of kTriangles
        try:
            drawManager.mesh(omr2.MUIDrawManager.kTris, pos_array, None, col_array)
        except Exception:
            log.debug("proxor mesh draw failed: %s", exc)


def _bbox_corners(loc: tuple, rot_y: float, mn: tuple, mx: tuple, normal: tuple = (0.0, 1.0, 0.0)) -> list[tuple]:
    # Local-space corners of the bbox plus an arrow tip vertex (index 8)
    # that projects forward on -Z from the bottom-front edge centre — small
    # "lip" matching the Blender add-on's draw_bbox() implementation.
    # The bbox is anchored at the centre of its bottom face so the cursor
    # hit point controls that anchor (matches Blender drag-to-place).
    width = mx[0] - mn[0]
    ox = (mn[0] + mx[0]) * 0.5
    oy = mn[1]
    oz = (mn[2] + mx[2]) * 0.5
    arrow_x = 0.0
    arrow_y = 0.0
    arrow_z = (mx[2] - oz) + width * 0.2  # forward on +Z (front face)
    local = [
        (mn[0] - ox, mn[1] - oy, mn[2] - oz),
        (mx[0] - ox, mn[1] - oy, mn[2] - oz),
        (mx[0] - ox, mn[1] - oy, mx[2] - oz),
        (mn[0] - ox, mn[1] - oy, mx[2] - oz),
        (mn[0] - ox, mx[1] - oy, mn[2] - oz),
        (mx[0] - ox, mx[1] - oy, mn[2] - oz),
        (mx[0] - ox, mx[1] - oy, mx[2] - oz),
        (mn[0] - ox, mx[1] - oy, mx[2] - oz),
        (arrow_x, arrow_y, arrow_z),
    ]
    return [_local_to_world(p, loc, rot_y, normal) for p in local]


# ---------------------------------------------------------------------------
# Locator node - does nothing on its own; the draw override does all the work
# ---------------------------------------------------------------------------


class BkPlacementLocator(omui2.MPxLocatorNode):
    # --- Attribute handles ---
    location_attr = None
    rotation_y_attr = None
    has_hit_attr = None
    hit_floor_attr = None
    surface_normal_attr = None
    download_state_attr = None
    bbox_min_attr = None
    bbox_max_attr = None

    @staticmethod
    def creator():
        return BkPlacementLocator()

    @staticmethod
    def initialize():
        nAttr = om2.MFnNumericAttribute()
        eAttr = om2.MFnEnumAttribute()

        BkPlacementLocator.location_attr = nAttr.createPoint("location", "loc")
        nAttr.storable = True
        nAttr.keyable = True
        nAttr.writable = True
        BkPlacementLocator.addAttribute(BkPlacementLocator.location_attr)

        BkPlacementLocator.rotation_y_attr = nAttr.create("rotationY", "rotY", om2.MFnNumericData.kDouble, 0.0)
        nAttr.storable = True
        nAttr.keyable = True
        nAttr.writable = True
        BkPlacementLocator.addAttribute(BkPlacementLocator.rotation_y_attr)

        BkPlacementLocator.has_hit_attr = nAttr.create("hasHit", "hasHit", om2.MFnNumericData.kBoolean, False)
        nAttr.storable = True
        nAttr.keyable = True
        nAttr.writable = True
        BkPlacementLocator.addAttribute(BkPlacementLocator.has_hit_attr)

        BkPlacementLocator.hit_floor_attr = nAttr.create("hitFloor", "hitFloor", om2.MFnNumericData.kBoolean, False)
        nAttr.storable = True
        nAttr.keyable = True
        nAttr.writable = True
        BkPlacementLocator.addAttribute(BkPlacementLocator.hit_floor_attr)

        # World-space surface normal at the raycast hit; (0,1,0) means
        # "use floor / Y-up orientation". Authored each cursor poll.
        BkPlacementLocator.surface_normal_attr = nAttr.createPoint("surfaceNormal", "srfNrm")
        nAttr.storable = True
        nAttr.keyable = True
        nAttr.writable = True
        nAttr.default = (0.0, 1.0, 0.0)
        BkPlacementLocator.addAttribute(BkPlacementLocator.surface_normal_attr)

        # Download state: 0=idle, 1=downloading, 2=done
        BkPlacementLocator.download_state_attr = eAttr.create("downloadState", "dlState", 0)
        eAttr.addField("idle", 0)
        eAttr.addField("downloading", 1)
        eAttr.addField("done", 2)
        eAttr.storable = True
        eAttr.keyable = True
        eAttr.writable = True
        BkPlacementLocator.addAttribute(BkPlacementLocator.download_state_attr)

        # bbox in Maya internal units (cm) - authored once at drag start.
        BkPlacementLocator.bbox_min_attr = nAttr.createPoint("bboxMin", "bbMin")
        nAttr.storable = True
        nAttr.keyable = True
        nAttr.writable = True
        BkPlacementLocator.addAttribute(BkPlacementLocator.bbox_min_attr)

        BkPlacementLocator.bbox_max_attr = nAttr.createPoint("bboxMax", "bbMax")
        nAttr.storable = True
        nAttr.keyable = True
        nAttr.writable = True
        BkPlacementLocator.addAttribute(BkPlacementLocator.bbox_max_attr)

        # Download progress 0..1 (used to drive a progress ring in the gizmo)
        BkPlacementLocator.download_progress_attr = nAttr.create(
            "downloadProgress", "dlProg", om2.MFnNumericData.kFloat, 0.0
        )
        nAttr.setMin(0.0)
        nAttr.setMax(1.0)
        nAttr.storable = True
        nAttr.keyable = True
        nAttr.writable = True
        BkPlacementLocator.addAttribute(BkPlacementLocator.download_progress_attr)

    def compute(self, plug, dataBlock):
        # No computation needed, but method must exist for dirty propagation
        return None


# ---------------------------------------------------------------------------
# Per-draw user data
# ---------------------------------------------------------------------------


class _BkUserData(om2.MUserData):
    """Snapshot of the placement state captured during prepareForDraw().

    Storing data on a per-call object decouples the draw thread (where
    addUIDrawables runs) from the main thread that mutates the state.
    """

    def __init__(self) -> None:
        # API 2.0: MUserData() takes no arguments; deleteAfterUse default True
        super().__init__()
        self.snap: dict | None = None


# ---------------------------------------------------------------------------
# Draw override - renders the bbox / proxor / hint
# ---------------------------------------------------------------------------


class BkPlacementDrawOverride(omr2.MPxDrawOverride):
    @staticmethod
    def creator(obj):
        return BkPlacementDrawOverride(obj)

    def __init__(self, obj) -> None:
        # geometryChangedCallback = None → we drive refresh via M3dView.refresh()
        super().__init__(obj, None, False)

    # ── Capabilities ──────────────────────────────────────────────────────

    def supportedDrawAPIs(self):
        return omr2.MRenderer.kOpenGL | omr2.MRenderer.kDirectX11 | omr2.MRenderer.kOpenGLCoreProfile

    def hasUIDrawables(self) -> bool:
        return True

    def isBounded(self, objPath, cameraPath) -> bool:
        # Unbounded so the locator is never frustum-culled even when at origin
        return False

    # ── Data preparation ─────────────────────────────────────────────────

    def prepareForDraw(self, objPath, cameraPath, frameContext, oldData):
        if isinstance(oldData, _BkUserData):  # noqa: SIM108
            data = oldData
        else:
            data = _BkUserData()

        # Read node attributes
        node = objPath.node()

        def _read_point3(attr):
            """Read a compound point3 (kDouble3) plug as a 3-tuple of floats."""
            p = om2.MPlug(node, attr)
            try:
                if p.isCompound and p.numChildren() >= 3:
                    return (
                        p.child(0).asDouble(),
                        p.child(1).asDouble(),
                        p.child(2).asDouble(),
                    )
            except Exception as exc:
                log.debug("point3 read failed: %s", exc)
            return (0.0, 0.0, 0.0)

        loc = _read_point3(BkPlacementLocator.location_attr)
        plug_rot = om2.MPlug(node, BkPlacementLocator.rotation_y_attr)
        rot_y = plug_rot.asDouble()
        plug_has_hit = om2.MPlug(node, BkPlacementLocator.has_hit_attr)
        has_hit = plug_has_hit.asBool()
        plug_hit_floor = om2.MPlug(node, BkPlacementLocator.hit_floor_attr)
        hit_floor = plug_hit_floor.asBool()
        try:
            surface_normal = _read_point3(BkPlacementLocator.surface_normal_attr)
        except Exception:
            surface_normal = (0.0, 1.0, 0.0)
        if surface_normal == (0.0, 0.0, 0.0):
            surface_normal = (0.0, 1.0, 0.0)
        plug_dl = om2.MPlug(node, BkPlacementLocator.download_state_attr)
        dl_state = plug_dl.asShort()
        plug_prog = om2.MPlug(node, BkPlacementLocator.download_progress_attr)
        dl_prog = max(0.0, min(1.0, float(plug_prog.asFloat())))
        bb_min = _read_point3(BkPlacementLocator.bbox_min_attr)
        bb_max = _read_point3(BkPlacementLocator.bbox_max_attr)

        # Look up proxor polylines stashed by the placement layer (by node name).
        try:
            node_name = om2.MFnDependencyNode(node).name()
        except Exception:
            node_name = ""
        proxor_lines = _state.get_proxor_lines(node_name)
        proxor_mesh = _state.get_proxor_mesh(node_name)
        label_entry = _state.get_label(node_name)

        # Compose snap dict for drawing
        data.snap = {
            "location": loc,
            "rotation_y": rot_y,
            "has_hit": has_hit,
            "hit_floor": hit_floor,
            "download_state": dl_state,
            "download_progress": dl_prog,
            "bbox_min": bb_min,
            "bbox_max": bb_max,
            "surface_normal": surface_normal,
            "proxor_lines": proxor_lines,
            "proxor_mesh": proxor_mesh,
            "label_name": label_entry.get("name", ""),
            "label_status": label_entry.get("status", ""),
            "active": True,
        }
        return data

    # ── Drawing ─────────────────────────────────────────────────────────

    def addUIDrawables(self, objPath, drawManager, frameContext, data) -> None:
        snap = getattr(data, "snap", None)
        if not snap or not snap.get("active"):
            return

        has_hit = bool(snap.get("has_hit"))
        on_floor = bool(snap.get("hit_floor"))
        dl_state = int(snap.get("download_state") or 0)
        dl_prog = float(snap.get("download_progress") or 0.0)
        downloading = dl_state == 1

        if downloading:
            col = om2.MColor((0.16, 0.42, 0.84, 1.0))  # Blendkit blue — downloading
        elif has_hit and on_floor:
            col = om2.MColor((0.39, 0.78, 1.00, 1.0))  # cyan  - floor plane
        elif has_hit:
            col = om2.MColor((0.00, 0.86, 0.31, 1.0))  # green - geometry hit
        else:
            col = om2.MColor((0.86, 0.20, 0.20, 1.0))  # red   - no surface (still draw, like Blender)

        loc = tuple(snap.get("location", (0.0, 0.0, 0.0)))
        rot_y = float(snap.get("rotation_y", 0.0))
        nrm = tuple(snap.get("surface_normal", (0.0, 1.0, 0.0)))
        # Defaults are in Maya internal units (cm).  A 1 m³ cube is the
        # fall-back so an asset with no bbox metadata is still clearly visible.
        bbox_min = tuple(snap.get("bbox_min", (-50.0, 0.0, -50.0)))
        bbox_max = tuple(snap.get("bbox_max", (50.0, 100.0, 50.0)))

        # Guarantee a visible volume even if bbox_min == bbox_max (some assets
        # in the API return zero-volume bbox).  Inflate to a 1 m cube (in cm).
        def _ensure_volume(mn, mx):
            if all(abs(mx[i] - mn[i]) < 1e-4 for i in range(3)):
                return (-50.0, 0.0, -50.0), (50.0, 100.0, 50.0)
            return mn, mx

        bbox_min, bbox_max = _ensure_volume(bbox_min, bbox_max)

        drawManager.beginDrawable()

        proxor = snap.get("proxor_lines") or []
        proxor_mesh = snap.get("proxor_mesh") or []
        has_proxor = bool(proxor) or (len(proxor_mesh) >= 3)
        cx, cy, cz = loc
        height = max(1e-3, bbox_max[1] - bbox_min[1])
        fill_top = bbox_min[1] + dl_prog * height  # local-space y cut-off

        # ── Bounding-box edges + orientation arrow lip ────────────────
        #  Hide the outer bbox when a proxor wireframe or mesh is available —
        #  the proxor itself communicates the shape better.
        if not has_proxor:
            drawManager.setColor(col)
            drawManager.setLineWidth(3.0)
            corners = [om2.MPoint(*c) for c in _bbox_corners(loc, rot_y, bbox_min, bbox_max, nrm)]
            for ai, bi in _BBOX_EDGES:
                drawManager.line(corners[ai], corners[bi])
            for ai, bi in _ARROW_EDGES:
                drawManager.line(corners[ai], corners[bi])

            # ── Progress fill: bottom slab up to ``dl_prog`` of bbox height ──
            if downloading and dl_prog > 0.0:
                fill_max = (bbox_max[0], fill_top, bbox_max[2])
                fill_corners = [om2.MPoint(*c) for c in _bbox_corners(loc, rot_y, bbox_min, fill_max, nrm)]
                fill_col = om2.MColor((0.30, 0.65, 1.00, 1.0))
                drawManager.setColor(fill_col)
                drawManager.setLineWidth(2.0)
                for ai, bi in _BBOX_EDGES:
                    drawManager.line(fill_corners[ai], fill_corners[bi])

        # Proxor / bbox share the asset's local frame, but the bbox is
        # drawn re-anchored to its bottom-centre so the cursor controls
        # that anchor. Apply the same shift to the proxor data so the
        # wireframe + hologram mesh line up with the cyan box (and with
        # the final imported geometry, which is also re-anchored on import).
        proxor_anchor = (
            (bbox_min[0] + bbox_max[0]) * 0.5,
            bbox_min[1],
            (bbox_min[2] + bbox_max[2]) * 0.5,
        )

        # ── Proxor hologram mesh (filled triangles) ─────────────────────
        if proxor_mesh and len(proxor_mesh) >= 3:
            shifted_mesh = [
                (v[0] - proxor_anchor[0], v[1] - proxor_anchor[1], v[2] - proxor_anchor[2]) for v in proxor_mesh
            ]
            _draw_proxor_mesh(
                drawManager,
                shifted_mesh,
                loc,
                rot_y,
                nrm,
                0.0,
                bbox_max[1] - bbox_min[1],
                dl_prog if downloading else 1.0,
                downloading,
            )

        # ── Proxor wireframe (optional) ─────────────────────────────────
        if proxor:
            ax0, ay0, az0 = proxor_anchor
            # Re-anchor polylines to bbox bottom-centre (see proxor mesh above).
            proxor_shifted = [[(p[0] - ax0, p[1] - ay0, p[2] - az0) for p in poly] for poly in proxor]
            # Fill-cutoff is now measured in the shifted frame (bottom = 0).
            fill_top_shifted = dl_prog * max(1e-3, bbox_max[1] - bbox_min[1])
            # Two-pass clip on the local-y threshold so the proxor visually
            # "fills up" as the download progresses.  Below the cut-off uses
            # the bright fill colour; above uses the muted outline colour.
            below_col = om2.MColor((0.30, 0.65, 1.00, 1.0)) if downloading else om2.MColor((1.0, 0.92, 0.42, 0.9))
            above_col = om2.MColor((0.55, 0.55, 0.55, 0.55))

            def _emit(seg_pts, color, width):
                drawManager.setColor(color)
                drawManager.setLineWidth(width)
                for p0, p1 in seg_pts:
                    ax, ay, az = _local_to_world(p0, loc, rot_y, nrm)
                    bx, by, bz = _local_to_world(p1, loc, rot_y, nrm)
                    drawManager.line(
                        om2.MPoint(ax, ay, az),
                        om2.MPoint(bx, by, bz),
                    )

            below_segs: list[tuple] = []
            above_segs: list[tuple] = []
            for polyline in proxor_shifted:
                if len(polyline) < 2:
                    continue
                for i in range(len(polyline) - 1):
                    a = polyline[i]
                    b = polyline[i + 1]
                    if not downloading:
                        below_segs.append((a, b))
                        continue
                    ya_below = a[1] <= fill_top_shifted
                    yb_below = b[1] <= fill_top_shifted
                    if ya_below and yb_below:
                        below_segs.append((a, b))
                    elif (not ya_below) and (not yb_below):
                        above_segs.append((a, b))
                    else:
                        denom = (b[1] - a[1]) or 1e-9
                        t = (fill_top_shifted - a[1]) / denom
                        t = max(0.0, min(1.0, t))
                        mid = (
                            a[0] + (b[0] - a[0]) * t,
                            fill_top_shifted,
                            a[2] + (b[2] - a[2]) * t,
                        )
                        if ya_below:
                            below_segs.append((a, mid))
                            above_segs.append((mid, b))
                        else:
                            above_segs.append((a, mid))
                            below_segs.append((mid, b))

            if above_segs:
                _emit(above_segs, above_col, 1.0)
            if below_segs:
                _emit(below_segs, below_col, 1.8)

        # ── Asset name + current step (3D text floating above the bbox) ─
        label_name = snap.get("label_name") or ""
        label_status = snap.get("label_status") or ""

        # MUIDrawManager.text() corrupts strings with any non-ASCII
        # codepoint (e.g. U+2026 "…") — it ends up rendering the whole
        # glyph atlas. Force pure ASCII.
        def _ascii(s: str) -> str:
            return s.encode("ascii", "replace").decode("ascii").replace("?", ".")

        label_name = _ascii(label_name)
        label_status = _ascii(label_status)
        if label_name or label_status:
            # Anchor a little above the bbox top so text doesn't z-fight
            # with the wireframe.
            top_y = bbox_max[1] + max(2.0, 0.05 * height)
            anchor = om2.MPoint(cx, cy + top_y, cz)
            try:
                drawManager.setFontSize(12)
            except Exception:
                pass
            if label_name:
                drawManager.setColor(om2.MColor((1.0, 1.0, 1.0, 1.0)))
                drawManager.text(anchor, label_name, omr2.MUIDrawManager.kCenter)
            if label_status:
                status_anchor = om2.MPoint(cx, cy + top_y - max(1.5, 0.03 * height), cz)
                try:
                    drawManager.setFontSize(10)
                except Exception:
                    pass
                drawManager.setColor(om2.MColor((0.75, 0.85, 1.0, 1.0)))
                drawManager.text(status_anchor, label_status, omr2.MUIDrawManager.kCenter)

        drawManager.endDrawable()


# ---------------------------------------------------------------------------
# Registration helpers (called from maya_plugin.py)
# ---------------------------------------------------------------------------


def register(plugin_fn: om2.MFnPlugin) -> None:
    """Register node + draw override.  Safe to call multiple times."""
    try:
        plugin_fn.registerNode(
            NODE_NAME,
            NODE_ID,
            BkPlacementLocator.creator,
            BkPlacementLocator.initialize,
            om2.MPxNode.kLocatorNode,
            DRAW_CLASSIFICATION,
        )
    except Exception as exc:
        log.warning("registerNode(%s) failed: %s", NODE_NAME, exc)

    try:
        omr2.MDrawRegistry.registerDrawOverrideCreator(
            DRAW_CLASSIFICATION,
            DRAW_REGISTRANT_ID,
            BkPlacementDrawOverride.creator,
        )
    except Exception as exc:
        log.warning("registerDrawOverrideCreator failed: %s", exc)

    log.debug("BkPlacementLocator registered")


def deregister(plugin_fn: om2.MFnPlugin) -> None:
    """Deregister node + draw override (called from uninitializePlugin)."""
    try:
        omr2.MDrawRegistry.deregisterDrawOverrideCreator(
            DRAW_CLASSIFICATION,
            DRAW_REGISTRANT_ID,
        )
    except Exception as exc:
        log.warning("deregisterDrawOverrideCreator failed: %s", exc)

    try:
        plugin_fn.deregisterNode(NODE_ID)
    except Exception as exc:
        log.warning("deregisterNode(%s) failed: %s", NODE_NAME, exc)


# ---------------------------------------------------------------------------
# Standalone plug-in entry points
# ---------------------------------------------------------------------------
# These let Maya load this file directly via Plug-in Manager (or via
# `cmds.loadPlugin("placement_locator")`).  The main Blendkit plug-in
# auto-loads this file on startup and sets autoload=True so it survives
# Maya restarts.


def initializePlugin(plugin) -> None:
    # load version info from the main Blendkit plug-in (if present) so the locator
    # shows the same version in Plug-in Manager.  If the main plug-in isn't loaded, fall back to a generic version string.
    try:
        from bk_maya import _version

        version = _version.__version__
    except Exception:
        version = "1.0"
    fn = om2.MFnPlugin(plugin, "Blendkit", version, "Any")
    register(fn)


def uninitializePlugin(plugin) -> None:
    fn = om2.MFnPlugin(plugin)
    deregister(fn)
