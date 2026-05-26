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
# Proxor registry
# ---------------------------------------------------------------------------
# Proxor data is a list of polylines — too large/structured to store as a
# Maya node attribute.  We stash it in a module-level dict keyed by the
# locator's node name; the draw override looks it up each frame.  The
# placement layer pushes to it when the locator is created and the
# download controller clears it on completion / failure.

_proxor_registry: dict[str, list] = {}


def set_proxor_lines(node_name: str, lines: list) -> None:
    """Register proxor polylines for *node_name* (use ``[]`` to clear)."""
    if lines:
        _proxor_registry[node_name] = lines
    else:
        _proxor_registry.pop(node_name, None)


def clear_proxor_lines(node_name: str) -> None:
    """Remove any proxor data registered for *node_name*."""
    _proxor_registry.pop(node_name, None)


# ---------------------------------------------------------------------------
# Label registry (asset name + current step)
# ---------------------------------------------------------------------------
# Same idea as the proxor registry — short strings shown as 3D text on top
# of the bbox helper while a download is running.

_label_registry: dict[str, dict] = {}


def set_label(node_name: str, name: str | None = None,
              status: str | None = None) -> None:
    """Update the on-screen labels for *node_name*.

    Pass only the keyword(s) you want to change; ``None`` leaves a field
    untouched.  Pass empty string to clear a field.
    """
    if not node_name:
        return
    entry = _label_registry.setdefault(node_name, {"name": "", "status": ""})
    if name is not None:
        entry["name"] = str(name)
    if status is not None:
        entry["status"] = str(status)


def clear_label(node_name: str) -> None:
    """Remove any label data registered for *node_name*."""
    _label_registry.pop(node_name, None)


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

# Edges for the orientation arrow: triangle at the front bottom edge (corners 2,3 are +Z).
# Index 8 is the tip, 2 and 3 are the front-bottom corners.
_ARROW_EDGES = [(2, 8), (3, 8), (2, 3)]


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
    arrow_z = (mx[2] - oz) + width * 0.2     # forward on +Z (front face)
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
        plug_prog = om2.MPlug(node, BkPlacementLocator.download_progress_attr)
        dl_prog = max(0.0, min(1.0, float(plug_prog.asFloat())))
        bb_min = _read_point3(BkPlacementLocator.bbox_min_attr)
        bb_max = _read_point3(BkPlacementLocator.bbox_max_attr)

        # Look up proxor polylines stashed by the placement layer (by node name).
        try:
            node_name = om2.MFnDependencyNode(node).name()
        except Exception:
            node_name = ""
        proxor_lines = _proxor_registry.get(node_name) or []
        label_entry  = _label_registry.get(node_name) or {}

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
            "proxor_lines": proxor_lines,
            "label_name":   label_entry.get("name", ""),
            "label_status": label_entry.get("status", ""),
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
        dl_state = int(snap.get("download_state") or 0)
        dl_prog  = float(snap.get("download_progress") or 0.0)
        downloading = dl_state == 1
        log.warning("[DRAW] has_hit=%s on_floor=%s dl_state=%s prog=%.2f",
                    has_hit, on_floor, dl_state, dl_prog)

        if downloading:
            col = om2.MColor((0.16, 0.42, 0.84, 1.0))    # BlenderKit blue — downloading
        elif has_hit and on_floor:
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

        proxor   = snap.get("proxor_lines") or []
        cx, cy, cz = loc
        height   = max(1e-3, bbox_max[1] - bbox_min[1])
        fill_top = bbox_min[1] + dl_prog * height       # local-space y cut-off

        # ── Bounding-box edges + orientation arrow lip ────────────────
        #  Hide the outer bbox when a proxor wireframe is available — the
        #  proxor itself communicates the shape better, and the user asked
        #  for "only the mesh, no glowing outline" in Maya.
        if not proxor:
            drawManager.setColor(col)
            drawManager.setLineWidth(3.0)
            corners = [om2.MPoint(*c) for c in _bbox_corners(loc, rot_y, bbox_min, bbox_max)]
            for ai, bi in _BBOX_EDGES:
                drawManager.line(corners[ai], corners[bi])
            for ai, bi in _ARROW_EDGES:
                drawManager.line(corners[ai], corners[bi])

            # ── Progress fill: bottom slab up to ``dl_prog`` of bbox height ──
            if downloading and dl_prog > 0.0:
                fill_max = (bbox_max[0], fill_top, bbox_max[2])
                fill_corners = [om2.MPoint(*c) for c in
                                _bbox_corners(loc, rot_y, bbox_min, fill_max)]
                fill_col = om2.MColor((0.30, 0.65, 1.00, 1.0))
                drawManager.setColor(fill_col)
                drawManager.setLineWidth(2.0)
                for ai, bi in _BBOX_EDGES:
                    drawManager.line(fill_corners[ai], fill_corners[bi])

        # ── Proxor wireframe (optional) ─────────────────────────────────
        if proxor:
            # Two-pass clip on the local-y threshold so the proxor visually
            # "fills up" as the download progresses.  Below the cut-off uses
            # the bright fill colour; above uses the muted outline colour.
            below_col = (om2.MColor((0.30, 0.65, 1.00, 1.0)) if downloading
                         else om2.MColor((1.0, 0.92, 0.42, 0.9)))
            above_col = om2.MColor((0.55, 0.55, 0.55, 0.55))

            def _emit(seg_pts, color, width):
                drawManager.setColor(color)
                drawManager.setLineWidth(width)
                for (p0, p1) in seg_pts:
                    ax, ay, az = _rotate_y(p0, rot_y)
                    bx, by, bz = _rotate_y(p1, rot_y)
                    drawManager.line(
                        om2.MPoint(cx + ax, cy + ay, cz + az),
                        om2.MPoint(cx + bx, cy + by, cz + bz),
                    )

            below_segs: list[tuple] = []
            above_segs: list[tuple] = []
            for polyline in proxor:
                if len(polyline) < 2:
                    continue
                for i in range(len(polyline) - 1):
                    a = polyline[i]
                    b = polyline[i + 1]
                    if not downloading:
                        below_segs.append((a, b))
                        continue
                    ya_below = a[1] <= fill_top
                    yb_below = b[1] <= fill_top
                    if ya_below and yb_below:
                        below_segs.append((a, b))
                    elif (not ya_below) and (not yb_below):
                        above_segs.append((a, b))
                    else:
                        denom = (b[1] - a[1]) or 1e-9
                        t = (fill_top - a[1]) / denom
                        t = max(0.0, min(1.0, t))
                        mid = (
                            a[0] + (b[0] - a[0]) * t,
                            fill_top,
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
        label_name   = snap.get("label_name")   or ""
        label_status = snap.get("label_status") or ""
        if label_name or label_status:
            # Anchor a little above the bbox top so text doesn't z-fight
            # with the wireframe.
            top_y  = bbox_max[1] + max(2.0, 0.05 * height)
            anchor = om2.MPoint(cx, cy + top_y, cz)
            try:
                drawManager.setFontSize(12)
            except Exception:
                pass
            if label_name:
                drawManager.setColor(om2.MColor((1.0, 1.0, 1.0, 1.0)))
                drawManager.text(anchor, label_name, omr2.MUIDrawManager.kCenter)
            if label_status:
                status_anchor = om2.MPoint(
                    cx, cy + top_y - max(1.5, 0.03 * height), cz
                )
                try:
                    drawManager.setFontSize(10)
                except Exception:
                    pass
                drawManager.setColor(om2.MColor((0.75, 0.85, 1.0, 1.0)))
                drawManager.text(status_anchor, label_status,
                                 omr2.MUIDrawManager.kCenter)

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
