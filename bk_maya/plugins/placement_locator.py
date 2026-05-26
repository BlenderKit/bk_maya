"""BlenderKit placement locator – Maya viewport draw override.

A custom `MPxLocatorNode` whose `MPxDrawOverride` renders the drag-to-place
visualisation directly with `MUIDrawManager`.

Visual elements
===============
*  Bounding-box edges (12 lines) – green when valid (geometry hit), cyan when
   falling back to the Y=0 floor, red when no surface.
*  Optional proxor wireframe – yellow line segments transformed by location +
   Y-rotation.
*  Screen-space hint text in the bottom centre (Wheel: rotate, etc.).

The locator is a *transient* node: created lazily when the drag session starts
and deleted on completion / cancellation.  It carries no data of its own –
:func:`bk_maya.ui.placement.get_drag_snapshot` is queried each draw cycle.
"""
from __future__ import annotations

import logging

import maya.api.OpenMaya as om2
import maya.api.OpenMayaRender as omr2
import maya.api.OpenMayaUI as omui2

log = logging.getLogger("bk_maya.placement_locator")


# ---------------------------------------------------------------------------
# Maya registration constants
# ---------------------------------------------------------------------------

NODE_NAME           = "bkPlacementLocator"
# Arbitrary node-id in the "developer-experimental" range (0x00120000-0x0012FFFF
# is normally safe; use 0x0013FA60 to avoid collisions).
NODE_ID             = om2.MTypeId(0x0013FA60)
DRAW_CLASSIFICATION = "drawdb/geometry/bkPlacementLocator"
DRAW_REGISTRANT_ID  = "BkPlacementLocatorPlugin"


def maya_useNewAPI() -> None:
    """Marker: this module uses Maya Python API 2.0."""


# ---------------------------------------------------------------------------
# Bounding-box helpers (duplicated here so the locator has no UI dependency)
# ---------------------------------------------------------------------------

import math

_BBOX_EDGES = [
    (0, 1), (1, 2), (2, 3), (3, 0),
    (4, 5), (5, 6), (6, 7), (7, 4),
    (0, 4), (1, 5), (2, 6), (3, 7),
]

# Edges from the forward-pointing arrow vertex (index 8) to the two front
# bottom corners (0 and 1) — the small "lip" that visualises orientation.
_ARROW_EDGES = [(0, 8), (1, 8)]


def _rotate_y(pt: tuple, deg: float) -> tuple:
    r = math.radians(deg)
    c, s = math.cos(r), math.sin(r)
    x, y, z = pt
    return (x * c - z * s, y, x * s + z * c)


def _bbox_corners(loc: tuple, rot_y: float, mn: tuple, mx: tuple) -> list[tuple]:
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
    arrow_z = (mn[2] - oz) - width * 0.5     # forward on -Z
    local = [
        (mn[0]-ox, mn[1]-oy, mn[2]-oz), (mx[0]-ox, mn[1]-oy, mn[2]-oz),
        (mx[0]-ox, mn[1]-oy, mx[2]-oz), (mn[0]-ox, mn[1]-oy, mx[2]-oz),
        (mn[0]-ox, mx[1]-oy, mn[2]-oz), (mx[0]-ox, mx[1]-oy, mn[2]-oz),
        (mx[0]-ox, mx[1]-oy, mx[2]-oz), (mn[0]-ox, mx[1]-oy, mx[2]-oz),
        (arrow_x,  arrow_y,  arrow_z),
    ]
    cx, cy, cz = loc
    out = []
    for lx, ly, lz in local:
        rx, ry, rz = _rotate_y((lx, ly, lz), rot_y)
        out.append((cx + rx, cy + ry, cz + rz))
    return out


# ---------------------------------------------------------------------------
# Locator node – does nothing on its own; the draw override does all the work
# ---------------------------------------------------------------------------

class BkPlacementLocator(omui2.MPxLocatorNode):
    # --- Attribute handles ---
    location_attr = None
    rotation_y_attr = None
    has_hit_attr = None
    hit_floor_attr = None
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

        # Download state: 0=idle, 1=downloading, 2=done
        BkPlacementLocator.download_state_attr = eAttr.create("downloadState", "dlState", 0)
        eAttr.addField("idle", 0)
        eAttr.addField("downloading", 1)
        eAttr.addField("done", 2)
        eAttr.storable = True
        eAttr.keyable = True
        eAttr.writable = True
        BkPlacementLocator.addAttribute(BkPlacementLocator.download_state_attr)

        # bbox in Maya internal units (cm) – authored once at drag start.
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
# Draw override – renders the bbox / proxor / hint
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
        return (
            omr2.MRenderer.kOpenGL
            | omr2.MRenderer.kDirectX11
            | omr2.MRenderer.kOpenGLCoreProfile
        )

    def hasUIDrawables(self) -> bool:
        return True

    def isBounded(self, objPath, cameraPath) -> bool:
        # Unbounded so the locator is never frustum-culled even when at origin
        return False

    # ── Data preparation ─────────────────────────────────────────────────

    def prepareForDraw(self, objPath, cameraPath, frameContext, oldData):
        if isinstance(oldData, _BkUserData):
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
        plug_dl = om2.MPlug(node, BkPlacementLocator.download_state_attr)
        dl_state = plug_dl.asShort()
        bb_min = _read_point3(BkPlacementLocator.bbox_min_attr)
        bb_max = _read_point3(BkPlacementLocator.bbox_max_attr)

        # Compose snap dict for drawing
        data.snap = {
            "location": loc,
            "rotation_y": rot_y,
            "has_hit": has_hit,
            "hit_floor": hit_floor,
            "download_state": dl_state,
            "bbox_min": bb_min,
            "bbox_max": bb_max,
            "active": True,
        }
        log.warning("[DRAW] prepareForDraw ATTR snap=%s", data.snap)
        return data

    # ── Drawing ─────────────────────────────────────────────────────────

    def addUIDrawables(self, objPath, drawManager, frameContext, data) -> None:
        snap = getattr(data, "snap", None)
        log.warning("[DRAW] addUIDrawables snap=%s", snap)
        if not snap or not snap.get("active"):
            return

        has_hit  = bool(snap.get("has_hit"))
        on_floor = bool(snap.get("hit_floor"))
        log.warning("[DRAW] has_hit=%s on_floor=%s", has_hit, on_floor)

        if has_hit and on_floor:
            col = om2.MColor((0.39, 0.78, 1.00, 1.0))    # cyan  – floor plane
        elif has_hit:
            col = om2.MColor((0.00, 0.86, 0.31, 1.0))    # green – geometry hit
        else:
            col = om2.MColor((0.86, 0.20, 0.20, 1.0))    # red   – no surface (still draw, like Blender)

        loc      = tuple(snap.get("location", (0.0, 0.0, 0.0)))
        rot_y    = float(snap.get("rotation_y", 0.0))
        log.warning("[DRAW] loc=%s rot_y=%s", loc, rot_y)
        # Defaults are in Maya internal units (cm).  A 1 m³ cube is the
        # fall-back so an asset with no bbox metadata is still clearly visible.
        bbox_min = tuple(snap.get("bbox_min", (-50.0,   0.0, -50.0)))
        bbox_max = tuple(snap.get("bbox_max", ( 50.0, 100.0,  50.0)))

        # Guarantee a visible volume even if bbox_min == bbox_max (some assets
        # in the API return zero-volume bbox).  Inflate to a 1 m cube (in cm).
        def _ensure_volume(mn, mx):
            if all(abs(mx[i] - mn[i]) < 1e-4 for i in range(3)):
                return (-50.0, 0.0, -50.0), (50.0, 100.0, 50.0)
            return mn, mx
        bbox_min, bbox_max = _ensure_volume(bbox_min, bbox_max)

        drawManager.beginDrawable()

        # ── Bounding-box edges + orientation arrow lip ────────────────
        drawManager.setColor(col)
        drawManager.setLineWidth(3.0)
        corners = [om2.MPoint(*c) for c in _bbox_corners(loc, rot_y, bbox_min, bbox_max)]
        for ai, bi in _BBOX_EDGES:
            drawManager.line(corners[ai], corners[bi])
        for ai, bi in _ARROW_EDGES:
            drawManager.line(corners[ai], corners[bi])

        # ── Proxor wireframe (optional) ─────────────────────────────────
        proxor = snap.get("proxor_lines") or []
        if proxor:
            drawManager.setColor(om2.MColor((1.0, 0.92, 0.42, 0.9)))
            drawManager.setLineWidth(1.5)
            cx, cy, cz = loc
            for polyline in proxor:
                if len(polyline) < 2:
                    continue
                for i in range(len(polyline) - 1):
                    ax, ay, az = _rotate_y(polyline[i],     rot_y)
                    bx, by, bz = _rotate_y(polyline[i + 1], rot_y)
                    drawManager.line(
                        om2.MPoint(cx + ax, cy + ay, cz + az),
                        om2.MPoint(cx + bx, cy + by, cz + bz),
                    )

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
# `cmds.loadPlugin("placement_locator")`).  The main BlenderKit plug-in
# auto-loads this file on startup and sets autoload=True so it survives
# Maya restarts.

def initializePlugin(plugin) -> None:  # noqa: D401  (Maya API entry point)
    fn = om2.MFnPlugin(plugin, "BlenderKit", "1.0", "Any")
    register(fn)


def uninitializePlugin(plugin) -> None:  # noqa: D401  (Maya API entry point)
    fn = om2.MFnPlugin(plugin)
    deregister(fn)
