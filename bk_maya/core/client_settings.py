"""Client settings synchronisation for the Blendkit Maya plugin.

The Client (see :mod:`bk_maya.core.client_lib`) owns a versioned settings store
and is the *absolute source of truth*. It broadcasts a Snapshot on every
``/report`` (task type ``settings``) carrying a monotonically increasing
``revision`` and an ``updated_at`` timecode.

Reconciliation rules (newest wins; the Client is authoritative):

* The Client has a value we lack        -> adopt it.
* We change a value locally             -> push it up to the Client.
* Both have a value                     -> the Client wins; we update ourselves.

Reads are debounced by ``revision`` (apply only when it grows). Writes go up via
:mod:`bk_maya.core.client_lib` and come straight back on the next report, so the
two sides always converge.

The one host-specific caveat is the Blender executable: Maya requires Blender
5.0+, so we only adopt (or publish) entries meeting that minimum — a 4.x path a
Blender add-on registered is ignored rather than blindly adopted.
"""

from __future__ import annotations

import logging
import os
import threading
from collections.abc import Callable
from typing import Any

from . import client_lib, global_vars
from . import prefs as _prefs_mod

log = logging.getLogger(__name__)

PLUGIN_NAME = "bk_maya"
BLENDER_EXECUTABLE = "blender"
_MIN_BLENDER_MAJOR = 5  # mirrors blender_runner.MIN_BLENDER_MAJOR

_lock = threading.Lock()
_applied_revision: int = -1
_last_snapshot: dict[str, Any] = {}
_offered_local_blender = False
_listeners: list[Callable[[], None]] = []


# ── Listeners / accessors ─────────────────────────────────────────────────────


def register_listener(cb: Callable[[], None]) -> None:
    """Register a callback fired after a newer snapshot is applied.

    Callbacks run on whichever thread delivered the snapshot (usually the report
    poller / GUI thread); UI code must marshal onto the GUI thread itself.
    """
    with _lock:
        if cb not in _listeners:
            _listeners.append(cb)


def unregister_listener(cb: Callable[[], None]) -> None:
    with _lock:
        if cb in _listeners:
            _listeners.remove(cb)


def last_snapshot() -> dict[str, Any]:
    """Return a shallow copy of the most recently applied settings snapshot."""
    with _lock:
        return dict(_last_snapshot)


def get_variable(variable: str, plugin: str = PLUGIN_NAME, default: str = "") -> str:
    """Read a stored variable from the last snapshot (Client is authoritative).

    An empty *plugin* reads from the global variables; otherwise from the
    per-plugin namespace.
    """
    with _lock:
        snap = _last_snapshot
    if plugin:
        return (snap.get("plugin_variables", {}).get(plugin, {}) or {}).get(variable, default)
    return (snap.get("global_variables", {}) or {}).get(variable, default)


# ── Read path (adopt) ─────────────────────────────────────────────────────────


def on_snapshot(snap: dict[str, Any]) -> None:
    """Apply an incoming settings Snapshot (called from the report poller).

    Debounced by ``revision``: snapshots older than or equal to the last applied
    one are ignored, so a repeated broadcast never re-applies.
    """
    try:
        revision = int(snap.get("revision", 0))
    except (TypeError, ValueError):
        revision = 0

    global _applied_revision, _last_snapshot
    with _lock:
        if revision <= _applied_revision:
            return
        _applied_revision = revision
        _last_snapshot = snap

    changed = _adopt(snap)
    if changed:
        try:
            _prefs_mod.prefs.save()
        except Exception:
            log.exception("Saving prefs after settings sync failed")

    for cb in _listeners:
        try:
            cb()
        except Exception:
            log.exception("Settings listener raised")

    # Offer any local values the Client does not yet have (bidirectional
    # exchange). Runs at most once per session and off this thread, since it may
    # probe Blender for its version.
    _maybe_offer_local_blender(snap)


def _adopt(snap: dict[str, Any]) -> bool:
    """Adopt Client-owned values into local state. Returns True if prefs changed."""
    changed = False

    # Shared server: the Client is authoritative for which server everyone uses.
    server = (snap.get("shared") or {}).get("server") or ""
    if server and server != global_vars.SERVER:
        log.info("Adopting server from Client: %s", server)
        global_vars.SERVER = server

    # Blender executable: adopt the Client's best entry meeting our minimum.
    best = _best_client_blender(snap)
    if best is not None:
        path, _version = best
        if path and path != _prefs_mod.prefs.blender_exe:
            log.info("Adopting Blender executable from Client: %s", path)
            _prefs_mod.prefs.blender_exe = path
            changed = True

    return changed


def _best_client_blender(snap: dict[str, Any]) -> tuple[str, str] | None:
    """Highest-version stored Blender meeting the minimum major, or ``None``."""
    entries = (snap.get("executables") or {}).get(BLENDER_EXECUTABLE) or []
    best: tuple[tuple[int, ...], str, str] | None = None  # (sortkey, path, version)
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        path = entry.get("path") or ""
        version = entry.get("version") or ""
        if not path:
            continue
        major = _version_major(version)
        if major is not None and major < _MIN_BLENDER_MAJOR:
            continue
        key = _version_key(version)
        if best is None or key > best[0]:
            best = (key, path, version)
    if best is None:
        return None
    return best[1], best[2]


def _version_major(version: str) -> int | None:
    try:
        return int(str(version).split(".")[0])
    except (ValueError, IndexError, AttributeError):
        return None


def _version_key(version: str) -> tuple[int, ...]:
    parts: list[int] = []
    for p in str(version).split("."):
        try:
            parts.append(int(p))
        except ValueError:
            parts.append(0)
    return tuple(parts)


# ── Write path (publish / offer) ──────────────────────────────────────────────


def publish_blender_executable(path: str) -> None:
    """Push a locally chosen Blender executable up to the Client (background).

    Called when the user picks a Blender path in the settings dialog. The change
    bumps the Client revision and comes back on the next report, keeping every
    plugin in sync.
    """
    global _offered_local_blender
    path = (path or "").strip()
    if not path:
        return
    with _lock:
        _offered_local_blender = True  # we are the newest source now
    threading.Thread(target=_publish_blender_worker, args=(path,), daemon=True).start()


def _maybe_offer_local_blender(snap: dict[str, Any]) -> None:
    """Publish our local Blender once if the Client has no suitable entry."""
    global _offered_local_blender
    with _lock:
        if _offered_local_blender:
            return
    if _best_client_blender(snap) is not None:
        with _lock:
            _offered_local_blender = True
        return
    local = (_prefs_mod.prefs.blender_exe or "").strip()
    if not local or not os.path.isfile(local):
        return
    with _lock:
        _offered_local_blender = True
    threading.Thread(target=_publish_blender_worker, args=(local,), daemon=True).start()


def _publish_blender_worker(path: str) -> None:
    if not os.path.isfile(path):
        return
    version = ""
    try:
        from . import blender_runner

        detected = blender_runner.query_blender_version(path)
        if detected is not None:
            if detected[0] < _MIN_BLENDER_MAJOR:
                log.debug("Not publishing Blender %s (< %d.0) to Client.", detected, _MIN_BLENDER_MAJOR)
                return
            version = f"{detected[0]}.{detected[1]}.{detected[2]}"
    except Exception:
        log.debug("Could not determine Blender version for %s", path, exc_info=True)

    try:
        snap = client_lib.set_executable(BLENDER_EXECUTABLE, path, version=version)
        log.info("Published Blender executable to Client: %s (v%s)", path, version or "?")
        if isinstance(snap, dict):
            on_snapshot(snap)
    except Exception:
        log.debug("Publishing Blender executable to Client failed", exc_info=True)
