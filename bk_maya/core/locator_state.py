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

# Per-locator labels ({"name": str, "status": str}).
label_registry: dict[str, dict[str, str]] = {}


def set_label(node_name: str, *, name: str | None = None,
              status: str | None = None) -> None:
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
