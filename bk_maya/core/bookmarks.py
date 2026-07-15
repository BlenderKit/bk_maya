"""Account-synced asset bookmarks for the Blendkit Maya plugin.

Bookmarks are stored server-side as a ``bookmarks`` rating (value ``1``) on the
user's Blendkit account — exactly like the Blender addon — so they are shared
across Blender, the website and Maya.

This module owns the local mirror of that state:
  * a set of bookmarked ``assetBaseId`` strings,
  * helpers to toggle a bookmark (optimistic local update + a
    ``send_rating`` call through the local Go client),
  * a listener list so the asset-bar UI can refresh badges when the set
    changes.

The authoritative set is (re)loaded via :func:`refresh`, which asks the client
to fetch the account's bookmarks; the result is delivered back through
``client_lib.dispatch_tasks`` → :func:`on_bookmarks_loaded` on the GUI/poller
thread.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable, Iterable

from . import auth, client_lib

log = logging.getLogger(__name__)

_lock = threading.Lock()
_bookmarked: set[str] = set()
_loaded = False

# Listeners are invoked (on the GUI/poller thread) whenever the bookmark set
# changes, so open tiles can refresh their badge.
_listeners: list[Callable[[], None]] = []


# ── Listener management ──────────────────────────────────────────────────────


def add_listener(cb: Callable[[], None]) -> None:
    if cb not in _listeners:
        _listeners.append(cb)


def remove_listener(cb: Callable[[], None]) -> None:
    try:
        _listeners.remove(cb)
    except ValueError:
        pass


def _notify() -> None:
    for cb in tuple(_listeners):
        try:
            cb()
        except Exception:
            log.exception("Bookmark listener raised")


# ── Queries ──────────────────────────────────────────────────────────────────


def is_bookmarked(asset_id: str) -> bool:
    if not asset_id:
        return False
    with _lock:
        return asset_id in _bookmarked


def all_ids() -> set[str]:
    with _lock:
        return set(_bookmarked)


def is_loaded() -> bool:
    return _loaded


# ── Mutations ────────────────────────────────────────────────────────────────


def set_bookmarked(asset_id: str, value: bool) -> None:
    """Bookmark (*value* True) or un-bookmark *asset_id*.

    Updates the local set immediately (optimistic) and pushes the change to the
    server through the client.  Does nothing if the user is not logged in.
    """
    if not asset_id:
        return
    if not auth.is_logged_in():
        log.info("Ignoring bookmark toggle — not logged in.")
        return

    with _lock:
        if value:
            _bookmarked.add(asset_id)
        else:
            _bookmarked.discard(asset_id)
    _notify()

    try:
        client_lib.ensure_running()
        client_lib.send_rating(asset_id, "bookmarks", 1 if value else 0, api_key=auth.get_api_key())
    except Exception as exc:
        log.warning("Could not send bookmark for %s: %s", asset_id, exc)


def toggle(asset_id: str) -> bool:
    """Flip the bookmark state of *asset_id*; return the new state."""
    new_state = not is_bookmarked(asset_id)
    set_bookmarked(asset_id, new_state)
    return new_state


# ── Server sync ──────────────────────────────────────────────────────────────


def refresh() -> None:
    """Ask the client to (re)load the account's bookmarks (no-op if logged out)."""
    if not auth.is_logged_in():
        return
    try:
        client_lib.ensure_running()
        client_lib.get_bookmarks(api_key=auth.get_api_key())
    except Exception as exc:
        log.warning("Could not request bookmarks: %s", exc)


def on_bookmarks_loaded(ids: Iterable[str]) -> None:
    """Replace the local set with the freshly fetched *ids* and notify listeners.

    Called by ``client_lib.dispatch_tasks`` on the GUI/poller thread.
    """
    global _loaded
    new_set = {i for i in ids if i}
    with _lock:
        _bookmarked.clear()
        _bookmarked.update(new_set)
        _loaded = True
    log.debug("Loaded %d bookmarks", len(new_set))
    _notify()


def clear() -> None:
    """Drop all cached bookmark state (e.g. on logout)."""
    global _loaded
    with _lock:
        _bookmarked.clear()
        _loaded = False
    _notify()
