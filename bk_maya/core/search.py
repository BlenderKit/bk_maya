"""Asset search for BlenderKit Maya plugin.

Runs searches on a background thread and delivers results via a callback.
All public functions are safe to call from the Maya main thread.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable  # noqa: F401 (Any used in _run closure)

from ..api import client as api
from ..core import auth

log = logging.getLogger(__name__)

# -------------------------------------------------------------------------
# Types
# -------------------------------------------------------------------------

ResultCallback = Callable[[list[dict[str, Any]], int, str], None]
"""Called with (results_list, total_count, next_url) when a search completes."""

ErrorCallback = Callable[[str], None]
"""Called with an error message string if the search fails."""

# -------------------------------------------------------------------------
# Active search state
# -------------------------------------------------------------------------

_current_lock = threading.Lock()
_current_thread: threading.Thread | None = None
_cancel_flag = threading.Event()


# -------------------------------------------------------------------------
# Public API
# -------------------------------------------------------------------------

def search(
    query: str = "",
    asset_type: str = "model",
    order: str = "",           # empty → auto-determined by client
    page_size: int = 24,
    page_offset: int = 0,
    free_only: bool = False,
    quality_limit: int = 0,    # show only quality >= this (0 = no limit)
    license_filter: str = "ANY",  # ANY, ROYALTY_FREE, FULL, USAGE_RIGHTS
    animated_only: bool = False,
    texture_res_min: int = 0,
    texture_res_max: int = 0,
    file_size_min: int = 0,    # MB
    file_size_max: int = 0,    # MB
    poly_count_min: int = 0,
    poly_count_max: int = 0,
    style: str = "ANY",        # REALISTIC, PAINTERLY, LOWPOLY, ANIME, 2D_VECTOR, 3D_GRAPHICS, OTHER, ANY
    condition: str = "UNSPECIFIED",  # UNSPECIFIED, NEW, USED, OLD, DESOLATE
    design_year_min: int = 0,
    design_year_max: int = 0,
    geometry_nodes: bool = False,
    next_url: str = "",        # cursor URL from previous page's response
    on_results: ResultCallback | None = None,
    on_error: ErrorCallback | None = None,
) -> None:
    """Start an async search.

    Any previous in-flight search is cancelled before the new one starts.
    *on_results* is called on the worker thread when results arrive; callers
    must marshal to the main thread themselves (e.g. via a Qt signal).
    """
    cancel()  # cancel any in-flight request

    _cancel_flag.clear()

    def _run() -> None:
        try:
            api_key = auth.get_api_key()
            extra: dict[str, Any] = {}
            if free_only:
                extra["is_free"] = "true"
            if quality_limit > 0:
                extra["quality_gte"] = quality_limit
            if license_filter != "ANY":
                extra["license"] = license_filter
            if animated_only:
                extra["animated"] = "True"  # capital T mirrors Blender's str(True)
            if texture_res_min > 0:
                extra["textureResolutionMax_gte"] = texture_res_min
            if texture_res_max > 0:
                extra["textureResolutionMax_lte"] = texture_res_max
            # file_size in MB → server expects bytes
            if file_size_min > 0:
                extra["files_size_gte"] = file_size_min * 1024 * 1024
            if file_size_max > 0:
                extra["files_size_lte"] = file_size_max * 1024 * 1024
            if poly_count_min > 0:
                extra["faceCount_gte"] = poly_count_min
            if poly_count_max > 0:
                extra["faceCount_lte"] = poly_count_max
            # Model-specific filters (server ignores irrelevant ones for other types)
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

            log.debug(
                "search params: free=%s quality=%d license=%s animated=%s "
                "tex=%d-%d file=%d-%d poly=%d-%d style=%s cond=%s year=%d-%d geo=%s | extra_params=%s",
                free_only, quality_limit, license_filter, animated_only,
                texture_res_min, texture_res_max, file_size_min, file_size_max,
                poly_count_min, poly_count_max, style, condition,
                design_year_min, design_year_max, geometry_nodes, extra
            )
            data = api.search(
                query=query,
                asset_type=asset_type,
                order=order,
                page_size=page_size,
                page_offset=page_offset,
                api_key=api_key,
                extra_params=extra or None,
                next_url=next_url,
            )
            if _cancel_flag.is_set():
                return
            results = data.get("results", [])
            count   = data.get("count", 0)
            nxt     = data.get("next") or ""
            if on_results:
                on_results(results, count, nxt)
        except Exception as exc:
            if not _cancel_flag.is_set():
                log.error("Search failed: %s", exc)
                if on_error:
                    on_error(str(exc))

    with _current_lock:
        global _current_thread
        t = threading.Thread(target=_run, daemon=True, name="bk-search")
        _current_thread = t
        t.start()


def cancel() -> None:
    """Cancel any in-flight search (best-effort)."""
    _cancel_flag.set()
    with _current_lock:
        global _current_thread
        _current_thread = None


def search_next_page(
    query: str,
    asset_type: str,
    current_count: int,
    page_size: int = 24,
    on_results: ResultCallback | None = None,
    on_error: ErrorCallback | None = None,
) -> None:
    """Convenience: fetch the next page based on already-loaded count."""
    search(
        query=query,
        asset_type=asset_type,
        page_size=page_size,
        page_offset=current_count,
        on_results=on_results,
        on_error=on_error,
    )
