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
    texture_res_min: int = 0,
    texture_res_max: int = 0,
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
            if texture_res_min > 0:
                extra["texture_resolution_min"] = texture_res_min
            if texture_res_max > 0:
                extra["texture_resolution_max"] = texture_res_max
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
