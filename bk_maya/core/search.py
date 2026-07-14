"""Asset search for the Blendkit Maya plugin.

Searches are dispatched through the local ``blenderkit-client`` process
(see ``core.client_lib``).  We POST the search URL and a tempdir; the
client fetches results from the Blendkit API, downloads thumbnails
into the tempdir, and reports progress through ``/report``.

This module is intentionally thin — the report poller in
``ui.asset_bar`` calls ``client_lib.dispatch_tasks``, which invokes the
callback registered here on completion.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from typing import Any

from ..api import client as api
from . import auth, client_lib
from . import prefs as _prefs_mod

log = logging.getLogger(__name__)

ResultCallback = Callable[[list[dict[str, Any]], int, str], None]
"""Called with ``(results, total_count, next_url)`` when results arrive."""

ErrorCallback = Callable[[str], None]


# ── tempdir per asset type (mirrors Blender's ``get_temp_dir``) ──────────────


def _tempdir_for(asset_type: str) -> str:
    base = os.path.join(_prefs_mod.prefs.global_dir_resolved(), f"{asset_type}_search")
    os.makedirs(base, exist_ok=True)
    return base


def get_tempdir(asset_type: str) -> str:
    """Public accessor for the per-asset-type thumbnail cache directory."""
    return _tempdir_for(asset_type)


# ── Cancellation state ───────────────────────────────────────────────────────

# We can't actually cancel an in-flight search on the client side, but we
# can drop its callback so stale results don't overwrite a newer search.
_active_task_id: str | None = None


def cancel() -> None:
    global _active_task_id
    if _active_task_id is not None:
        client_lib.search_registry.pop(_active_task_id)
        _active_task_id = None


# ── Public API ───────────────────────────────────────────────────────────────


def search(
    query: str = "",
    asset_type: str = "model",
    order: str = "",
    page_size: int = 24,
    page_offset: int = 0,  # unused (kept for callers' compatibility)
    free_only: bool = False,
    my_assets_only: bool = False,
    bookmarked_only: bool = False,
    quality_limit: int = 0,
    license_filter: str = "ANY",
    animated_only: bool = False,
    texture_res_min: int = 0,
    texture_res_max: int = 0,
    file_size_min: int = 0,
    file_size_max: int = 0,
    poly_count_min: int = 0,
    poly_count_max: int = 0,
    style: str = "ANY",
    condition: str = "UNSPECIFIED",
    design_year_min: int = 0,
    design_year_max: int = 0,
    geometry_nodes: bool = False,
    next_url: str = "",
    extra_filters: dict[str, Any] | None = None,
    on_results: ResultCallback | None = None,
    on_error: ErrorCallback | None = None,
) -> None:
    """Start a search via the local client.

    Results are delivered to *on_results* by the report poller (main thread).
    Any previous in-flight search's callback is dropped first so that a
    late-arriving result for an older query cannot overwrite the new one.
    """
    global _active_task_id
    cancel()

    extra: dict[str, Any] = {}
    if free_only:
        extra["is_free"] = "true"
    if quality_limit > 0:
        extra["quality_gte"] = quality_limit
    if license_filter != "ANY":
        extra["license"] = license_filter
    if animated_only:
        extra["animated"] = "True"
    if texture_res_min > 0:
        extra["textureResolutionMax_gte"] = texture_res_min
    if texture_res_max > 0:
        extra["textureResolutionMax_lte"] = texture_res_max
    if file_size_min > 0:
        extra["files_size_gte"] = file_size_min * 1024 * 1024
    if file_size_max > 0:
        extra["files_size_lte"] = file_size_max * 1024 * 1024
    if poly_count_min > 0:
        extra["faceCount_gte"] = poly_count_min
    if poly_count_max > 0:
        extra["faceCount_lte"] = poly_count_max
    if style != "ANY":
        extra["modelStyle"] = style
    if condition != "UNSPECIFIED":
        extra["condition"] = condition
    if design_year_min > 0:
        extra["designYear_gte"] = design_year_min
    if design_year_max > 0:
        extra["designYear_lte"] = design_year_max
    if geometry_nodes:
        extra["modifiers"] = "nodes"
    if bookmarked_only:
        extra["bookmarks_rating"] = 1
    if extra_filters:
        extra.update(extra_filters)

    # "My assets only" constrains results to the logged-in user's author_id.
    # An explicit author_id (e.g. from "Search by author") always takes
    # precedence, mirroring the Blender addon's behaviour.
    if my_assets_only and "author_id" not in extra:
        uid = auth.get_user_id()
        if uid:
            extra["author_id"] = uid

    urlquery = api.build_search_url(
        query=query,
        asset_type=asset_type,
        order=order,
        page_size=page_size,
        extra_params=extra or None,
        next_url=next_url,
    )

    try:
        # Explicit user action: clear any "client unavailable" latch so a
        # freshly built/installed client is retried without restarting Maya.
        client_lib.reset_availability()
        client_lib.ensure_running()
    except Exception as exc:
        log.error("Cannot start Blendkit client: %s", exc)
        if on_error:
            on_error(str(exc))
        return

    api_key = auth.get_api_key()
    tempdir = _tempdir_for(asset_type)

    def _on_result(result: dict[str, Any]) -> None:
        global _active_task_id
        _active_task_id = None
        if on_results is None:
            return
        results = result.get("results") or []
        count = int(result.get("count") or 0)
        nxt = result.get("next") or ""
        on_results(results, count, nxt)

    def _on_error(msg: str) -> None:
        global _active_task_id
        _active_task_id = None
        log.error("Search task failed: %s", msg)
        if on_error:
            on_error(msg)

    try:
        task_id = client_lib.asset_search(
            urlquery=urlquery,
            tempdir=tempdir,
            asset_type=asset_type,
            api_key=api_key,
            page_size=page_size,
            next_url=next_url,
            get_next=bool(next_url),
        )
    except Exception as exc:
        log.error("asset_search POST failed: %s", exc)
        if on_error:
            on_error(str(exc))
        return

    _active_task_id = task_id
    client_lib.search_registry.register(task_id, _on_result, _on_error)
    log.debug("Search dispatched: task_id=%s urlquery=%s", task_id, urlquery)


def search_next_page(
    query: str,
    asset_type: str,
    current_count: int,  # unused; here for backwards compatibility
    page_size: int = 24,
    on_results: ResultCallback | None = None,
    on_error: ErrorCallback | None = None,
) -> None:
    search(
        query=query,
        asset_type=asset_type,
        page_size=page_size,
        on_results=on_results,
        on_error=on_error,
    )
