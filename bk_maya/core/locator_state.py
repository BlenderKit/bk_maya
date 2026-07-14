"""Shared mutable state for the placement locator.

Maya loads ``bk_maya/plugins/placement_locator.py`` via ``cmds.loadPlugin``,
which executes the file as a *plug-in* with its own private module instance.
The rest of the addon imports the same file via the regular Python import
system (``from bk_maya.plugins import placement_locator``).  Those two paths
produce two different module objects with independent globals — so a dict
defined at module level inside ``placement_locator.py`` is unreachable from
the addon side.

This module exists solely to give both sides a single, shared object to
read from and write into.
"""

from __future__ import annotations

# Per-locator proxor wire-frame snapshots (list of polylines).
proxor_registry: dict[str, list] = {}

# Per-locator proxor mesh snapshots (flat list of triangle vertices,
# local-space, already axis-swapped to Maya Y-up and scaled to
# internal units). 3 consecutive verts = 1 triangle.
proxor_mesh_registry: dict[str, list] = {}

# Per-locator labels ({"name": str, "status": str}).
label_registry: dict[str, dict[str, str]] = {}

# Per-locator cancel callbacks. The download controller registers a
# zero-arg callable here so the viewport [X] badge (drawn by the locator's
# draw override) can be wired to an abort. The draw override only shows the
# badge when a callback is present for its node.
cancel_registry: dict[str, object] = {}


def set_cancel_callback(node_name: str, cb) -> None:
    if not node_name:
        return
    cancel_registry[node_name] = cb


def clear_cancel_callback(node_name: str) -> None:
    cancel_registry.pop(node_name, None)


def get_cancel_callback(node_name: str):
    return cancel_registry.get(node_name)


def has_cancel_callback(node_name: str) -> bool:
    return node_name in cancel_registry


def gizmo_anchors(loc, bbox_min, bbox_max):
    """Return world-space anchors for the gizmo's floating label + [X] badge.

    Shared by the draw override (to place the text/badge) and the download
    controller's click handler (to hit-test the badge), so both agree on
    where the cancel button lives.

    Returns ``(name_anchor, status_anchor, badge_anchor)`` as ``(x, y, z)``
    tuples in world space.
    """
    cx, cy, cz = loc
    height = max(1e-3, bbox_max[1] - bbox_min[1])
    width = max(1e-3, bbox_max[0] - bbox_min[0])
    top_y = bbox_max[1] + max(2.0, 0.05 * height)
    name_anchor = (cx, cy + top_y, cz)
    status_anchor = (cx, cy + top_y - max(1.5, 0.03 * height), cz)
    badge_anchor = (cx + width * 0.5 + max(3.0, 0.12 * width), cy + top_y, cz)
    return name_anchor, status_anchor, badge_anchor


def set_label(node_name: str, *, name: str | None = None, status: str | None = None) -> None:
    if not node_name:
        return
    entry = label_registry.setdefault(node_name, {"name": "", "status": ""})
    if name is not None:
        entry["name"] = str(name)
    if status is not None:
        entry["status"] = str(status)


def clear_label(node_name: str) -> None:
    label_registry.pop(node_name, None)


def get_label(node_name: str) -> dict[str, str]:
    return label_registry.get(node_name) or {"name": "", "status": ""}


def set_proxor_lines(node_name: str, lines: list) -> None:
    if not node_name:
        return
    proxor_registry[node_name] = lines


def clear_proxor_lines(node_name: str) -> None:
    proxor_registry.pop(node_name, None)


def get_proxor_lines(node_name: str) -> list:
    return proxor_registry.get(node_name) or []


def set_proxor_mesh(node_name: str, verts: list) -> None:
    if not node_name:
        return
    proxor_mesh_registry[node_name] = verts


def clear_proxor_mesh(node_name: str) -> None:
    proxor_mesh_registry.pop(node_name, None)


def get_proxor_mesh(node_name: str) -> list:
    return proxor_mesh_registry.get(node_name) or []
