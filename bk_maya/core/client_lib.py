"""BlenderKit-Client process integration.

Mirrors the architecture used by the Blender addon's ``client_lib.py``:
the addon never talks to ``blenderkit.com`` directly for search /
thumbnail / download work.  Instead it spawns a local Go process
(``blenderkit-client``) and talks to it over loopback HTTP.

The client:
  * fetches search results from the BlenderKit API,
  * downloads all thumbnails (rate-limited, 6 concurrent),
  * writes them into a caller-supplied ``tempdir``,
  * reports task progress through a polling ``/report`` endpoint.

This module owns:
  * launching / probing the client process,
  * port discovery,
  * the ``/blender/asset_search`` POST,
  * the ``/report`` GET poll,
  * a task-id → callback registry used by ``ui.asset_bar`` to deliver
    search results and thumbnail paths back to the GUI thread.
"""

from __future__ import annotations

import json
import logging
import os
import platform
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable

from . import global_vars

log = logging.getLogger(__name__)

# ── Versions / constants ─────────────────────────────────────────────────────

CLIENT_VERSION = "v1.9.0"
"""Bundled client binary version. Must match a folder under ``<addon>/client/``."""

API_VERSION = ".".join(CLIENT_VERSION.split(".")[:2])  # → "v1.9"

# Same ordering as the Blender addon; these are also the redirect_uri ports
# whitelisted by the OAuth app, so we cannot pick arbitrary ones.
CLIENT_PORTS: tuple[str, ...] = (
    "62485", "65425", "55428", "49452", "35452", "25152", "5152", "1234",
)

ADDON_VERSION = "0.1.0"
SOFTWARE_NAME = "Maya"

OAUTH_CLIENT_ID = "IdFRwa3SGA8eMpzhRVFMg5Ts8sPK93xBjif93x0F"
"""OAuth client id baked into the Go client; reused here for URL building."""

POLL_CONNECT_TIMEOUT = 0.20
POLL_READ_TIMEOUT    = 0.50
REQUEST_TIMEOUT      = 5.0

# ── Module state ─────────────────────────────────────────────────────────────

_state_lock      = threading.Lock()
_process: subprocess.Popen | None = None
_active_port:    str  = CLIENT_PORTS[0]
_app_id:         int  = os.getpid()


# ── Path helpers ─────────────────────────────────────────────────────────────

def _addon_root() -> str:
    """Return the directory that contains the ``client/`` binaries folder.

    In the source checkout this is the workspace root; in the installed
    addon it is the parent of ``bk_maya/``.  Both layouts ship a
    ``client/v<version>/`` directory.
    """
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _binary_name() -> str:
    """Match ``decide_client_binary_name`` from the Blender addon."""
    os_name = platform.system().lower()
    if os_name == "darwin":
        os_name = "macos"

    arch = platform.machine().lower()
    if arch == "amd64":
        arch = "x86_64"
    elif arch == "aarch64":
        arch = "arm64"

    name = f"blenderkit-client-{os_name}-{arch}"
    if os_name == "windows":
        name += ".exe"
    return name


def _binary_path() -> str:
    return os.path.join(_addon_root(), "client", CLIENT_VERSION, _binary_name())


def _log_path() -> str:
    import tempfile
    log_dir = os.path.join(tempfile.gettempdir(), "bk_maya_client")
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, f"client-{_active_port}.log")


# ── URL helpers ──────────────────────────────────────────────────────────────

def get_base_url(port: str | None = None) -> str:
    return f"http://127.0.0.1:{port or _active_port}/{API_VERSION}"


def get_app_id() -> int:
    return _app_id


# ── HTTP helpers ─────────────────────────────────────────────────────────────

def _http_request(
    method: str,
    url: str,
    body: dict | None = None,
    *,
    connect_timeout: float = REQUEST_TIMEOUT,
    read_timeout:    float = REQUEST_TIMEOUT,
) -> Any:
    """Minimal JSON-in / JSON-out HTTP call. Returns parsed JSON or None."""
    data = None
    headers = {"Accept": "application/json"}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"

    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    # urllib doesn't separate connect/read timeouts; use the larger of the two.
    with urllib.request.urlopen(req, timeout=max(connect_timeout, read_timeout)) as resp:
        raw = resp.read()
        if not raw:
            return None
        return json.loads(raw.decode("utf-8"))


# ── Process launch ───────────────────────────────────────────────────────────

def _ping(port: str) -> bool:
    """True if a client is responsive on *port*."""
    try:
        _http_request(
            "GET",
            f"http://127.0.0.1:{port}/{API_VERSION}/report",
            body=_minimal_report_data(),
            connect_timeout=POLL_CONNECT_TIMEOUT,
            read_timeout=POLL_READ_TIMEOUT,
        )
        return True
    except Exception:
        return False


def _find_running_client() -> str | None:
    for port in CLIENT_PORTS:
        if _ping(port):
            return port
    return None


def _spawn(port: str) -> subprocess.Popen:
    binary = _binary_path()
    if not os.path.isfile(binary):
        raise FileNotFoundError(
            f"BlenderKit client binary not found at {binary}. "
            f"Run dev.py to build/copy binaries."
        )

    creation_flags = 0
    if sys.platform == "win32":
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    log_file = open(_log_path(), "ab")
    args = [
        binary,
        "--port", port,
        "--server", global_vars.SERVER,
        "--proxy_which", "",
        "--proxy_address", "",
        "--trusted_ca_certs", "",
        "--ssl_context", "",
        "--version", f"{ADDON_VERSION}.0",
        "--software", SOFTWARE_NAME,
        "--pid", str(_app_id),
    ]
    log.info("Spawning BlenderKit client: %s", " ".join(args))
    return subprocess.Popen(
        args,
        stdout=log_file,
        stderr=log_file,
        creationflags=creation_flags,
        close_fds=(sys.platform != "win32"),
    )


def ensure_running(timeout: float = 8.0) -> str:
    """Make sure a client process is reachable; return its port.

    Thread-safe.  No-op if a process is already responsive.
    """
    global _process, _active_port

    with _state_lock:
        # Already-running existing process (perhaps from a previous Maya session)?
        existing = _find_running_client()
        if existing:
            _active_port = existing
            log.debug("Reusing client on port %s", existing)
            return existing

        # Spawn a new one on the preferred port and wait for it.
        port = CLIENT_PORTS[0]
        _process = _spawn(port)
        _active_port = port

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if _ping(port):
            log.info("Client ready on port %s", port)
            return port
        time.sleep(0.15)

    raise RuntimeError(
        f"BlenderKit client did not respond on port {port} within {timeout}s "
        f"(see log at {_log_path()})"
    )


def shutdown() -> None:
    """Best-effort: ask the client to stop and reap the process."""
    global _process
    try:
        _http_request("GET", f"{get_base_url()}/shutdown", body=_minimal_report_data())
    except Exception as exc:
        log.debug("Client shutdown request failed: %s", exc)
    with _state_lock:
        if _process is not None:
            try:
                _process.terminate()
            except Exception:
                pass
            _process = None


# ── Request payload helpers ──────────────────────────────────────────────────

def _minimal_report_data(api_key: str = "") -> dict[str, Any]:
    """Smallest payload accepted by ``/report`` — also used as a ping."""
    return {
        "app_id":           _app_id,
        "api_key":          api_key,
        "addon_version":    ADDON_VERSION,
        "platform_version": platform.platform(),
        "project_name":     "",
    }


def _prefs_block(api_key: str) -> dict[str, Any]:
    return {
        "api_key":              api_key,
        "api_key_refresh":      "",
        "api_key_timeout":      0,
        "scene_id":             "",
        "app_id":               _app_id,
        "unpack_files":         False,
        "create_asset_library": False,
        "resolution":           "ORIGINAL",
        "project_subdir":       "",
        "global_dir":           "",
        "binary_path":          "",
        "addon_dir":            "",
        "addon_module_name":    "bk_maya",
    }


# ── Search ───────────────────────────────────────────────────────────────────

# ── OAuth ip──────────────────────────────────────────────────────────────────────────────────────

def send_oauth_verification_data(code_verifier: str, state: str) -> None:
    """Hand the PKCE verifier + state to the client so it can complete the
    redirect-callback exchange when the browser hits ``/consumer/exchange/``.
    """
    body = _minimal_report_data()
    body["code_verifier"] = code_verifier
    body["state"]         = state
    _http_request("POST", f"{get_base_url()}/oauth2/verification_data", body=body)


def refresh_token(refresh_token_str: str, old_api_key: str = "") -> None:
    """Ask the client to refresh ``refresh_token_str``.  The new tokens come
    back as a ``login`` task on ``/report``.
    """
    body = _minimal_report_data(api_key=old_api_key)
    body["refresh_token"] = refresh_token_str
    _http_request("GET", f"{get_base_url()}/refresh_token", body=body)


def oauth2_logout(refresh_token_str: str, api_key: str = "") -> None:
    """Revoke tokens on the server via the client."""
    body = _minimal_report_data(api_key=api_key)
    body["refresh_token"] = refresh_token_str
    _http_request("GET", f"{get_base_url()}/oauth2/logout", body=body)


# ── Search ──────────────────────────────────────────────────────────────────────────────────────────────

def asset_search(
    *,
    urlquery:    str,
    tempdir:     str,
    asset_type:  str,
    api_key:     str = "",
    page_size:   int = 24,
    next_url:    str = "",
    get_next:    bool = False,
    scene_uuid:  str = "",
) -> str:
    """POST a search to the local client. Returns the task_id immediately.

    Actual results arrive via ``/report`` as a task of type ``search``.
    """
    body = {
        "PREFS":            _prefs_block(api_key),
        "addon_version":    ADDON_VERSION,
        "platform_version": platform.platform(),
        "api_key":          api_key,
        "app_id":           _app_id,
        "asset_type":       asset_type,
        "blender_version":  "0.0.0",   # client just echoes this back
        "get_next":         get_next,
        "next":             next_url,
        "page_size":        page_size,
        "scene_uuid":       scene_uuid or str(uuid.uuid4()),
        "tempdir":          tempdir,
        "urlquery":         urlquery,
        "is_validator":     False,
        "history_id":       "",
    }
    url = f"{get_base_url()}/blender/asset_search"
    resp = _http_request("POST", url, body=body)
    if not isinstance(resp, dict) or "task_id" not in resp:
        raise RuntimeError(f"Unexpected /asset_search response: {resp!r}")
    return resp["task_id"]


# ── Reports ──────────────────────────────────────────────────────────────────

def get_reports(api_key: str = "") -> list[dict[str, Any]]:
    """Poll ``/report`` and return the list of task dicts.

    The client deletes finished tasks after reporting them once, so any
    given completed task is delivered exactly one time.
    """
    url = f"{get_base_url()}/report"
    try:
        resp = _http_request(
            "GET", url, body=_minimal_report_data(api_key),
            connect_timeout=POLL_CONNECT_TIMEOUT,
            read_timeout=POLL_READ_TIMEOUT,
        )
    except urllib.error.URLError as exc:
        log.debug("Report poll failed: %s", exc)
        return []
    except Exception as exc:
        log.debug("Report poll error: %s", exc)
        return []
    if not isinstance(resp, list):
        return []
    return resp


# ── Task callback registry ───────────────────────────────────────────────────
#
# A search request returns a task_id immediately; the caller registers a
# pair of callbacks for that id, and the UI's report poller dispatches the
# matching ``search`` task back to them when the client completes it.
#
# Thumbnail tasks are not keyed by task_id (they're per-asset) and are
# instead dispatched by ``assetBaseId`` via ``ThumbRegistry``.

SearchCallback = Callable[[dict[str, Any]], None]   # (result dict)
ErrorCallback  = Callable[[str], None]


class _SearchRegistry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cbs: dict[str, tuple[SearchCallback, ErrorCallback | None]] = {}

    def register(
        self,
        task_id: str,
        on_result: SearchCallback,
        on_error: ErrorCallback | None = None,
    ) -> None:
        with self._lock:
            self._cbs[task_id] = (on_result, on_error)

    def pop(self, task_id: str) -> tuple[SearchCallback, ErrorCallback | None] | None:
        with self._lock:
            return self._cbs.pop(task_id, None)

    def clear(self) -> None:
        with self._lock:
            self._cbs.clear()


search_registry = _SearchRegistry()


class _ThumbRegistry:
    """Map ``assetBaseId`` → callback for thumbnail download notifications."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cbs: dict[str, Callable[[str], None]] = {}

    def register(self, asset_base_id: str, cb: Callable[[str], None]) -> None:
        if not asset_base_id:
            return
        with self._lock:
            self._cbs[asset_base_id] = cb

    def unregister(self, asset_base_id: str) -> None:
        with self._lock:
            self._cbs.pop(asset_base_id, None)

    def get(self, asset_base_id: str) -> Callable[[str], None] | None:
        with self._lock:
            return self._cbs.get(asset_base_id)

    def clear(self) -> None:
        with self._lock:
            self._cbs.clear()


thumb_registry = _ThumbRegistry()


LoginCallback = Callable[[dict[str, Any], str, str], None]
"""(result_dict, status, message) — status is 'finished' or 'error'."""

_login_lock = threading.Lock()
_login_cb: LoginCallback | None = None


def set_login_callback(cb: LoginCallback | None) -> None:
    """Register the callback invoked when a ``login`` task is reported.

    Only one callback is active at a time — used by ``core.auth``.
    """
    global _login_cb
    with _login_lock:
        _login_cb = cb


def dispatch_tasks(tasks: list[dict[str, Any]]) -> None:
    """Route completed tasks to the appropriate registered callback.

    Safe to call from the GUI thread (which is where the poller runs).
    """
    for task in tasks:
        ttype  = task.get("task_type", "")
        status = task.get("status", "")

        if ttype == "search":
            if status not in ("finished", "error"):
                continue
            entry = search_registry.pop(task.get("task_id", ""))
            if entry is None:
                continue
            on_result, on_error = entry
            if status == "finished":
                result = task.get("result") or {}
                try:
                    on_result(result)
                except Exception:
                    log.exception("Search result callback raised")
            else:
                msg = task.get("message") or "Search failed"
                if on_error:
                    try:
                        on_error(msg)
                    except Exception:
                        log.exception("Search error callback raised")

        elif ttype == "thumbnail_download":
            if status != "finished":
                continue
            data = task.get("data") or {}
            base_id = data.get("assetBaseId") or ""
            path    = data.get("image_path")  or ""
            if not (base_id and path):
                continue
            cb = thumb_registry.get(base_id)
            if cb is None:
                continue
            try:
                cb(path)
            except Exception:
                log.exception("Thumbnail callback raised for %s", base_id)

        elif ttype == "login":
            with _login_lock:
                cb = _login_cb
            if cb is None:
                continue
            result  = task.get("result") or {}
            message = task.get("message") or ""
            try:
                cb(result, status, message)
            except Exception:
                log.exception("Login callback raised")
