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

import atexit
import json
import logging
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from typing import Any, Callable

from . import global_vars
from . import prefs as _prefs_mod

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

ADDON_VERSION = "3.20.0"
"""Maya plugin version (X.Y.Z). Kept in sync with ``blender_manifest.toml``
so the Go client logs/reports the same version string as the Blender addon
would."""

ADDON_BUILD = "260517"
"""Date stamp (YYMMDD) used as the 4th version segment passed to the client."""

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

# Consecutive /report failures — used by the report poller to trigger an
# auto-respawn (mirrors Blender's CLIENT_FAILED_REPORTS counter).
_failed_reports: int = 0
_RESPAWN_AFTER_FAILURES = 10

# Installed binary copy, populated lazily by ``_binary_path`` so a read-only
# addon directory still works (Microsoft Store Maya, sandboxed installs).
_use_inplace_client: bool = False


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


def _inplace_binary_path() -> str:
    """Binary shipped inside the addon (``<addon>/client/vX.Y.Z/<name>``)."""
    return os.path.join(_addon_root(), "client", CLIENT_VERSION, _binary_name())


def _installed_binary_dir() -> str:
    """User-writable install location, mirrors Blender's
    ``<global_dir>/client/bin/vX.Y.Z/``.
    """
    return os.path.join(
        _prefs_mod.prefs.global_dir_resolved(), "client", "bin", CLIENT_VERSION,
    )


def _installed_binary_path() -> str:
    return os.path.join(_installed_binary_dir(), _binary_name())


def _client_source_dir() -> str:
    return os.path.join(_addon_root(), "client")


def _go_target() -> tuple[str, str]:
    """Return (GOOS, GOARCH) for the current platform."""
    sys_os = platform.system().lower()
    goos = "darwin" if sys_os == "darwin" else sys_os  # windows | linux | darwin
    arch = platform.machine().lower()
    goarch = {"amd64": "amd64", "x86_64": "amd64", "aarch64": "arm64", "arm64": "arm64"}.get(arch, arch)
    return goos, goarch


def _sources_newer_than(binary_path: str) -> bool:
    if not os.path.isfile(binary_path):
        return True
    bin_mtime = os.path.getmtime(binary_path)
    src_dir = _client_source_dir()
    for root, _dirs, files in os.walk(src_dir):
        # skip prebuilt binary version folders
        if os.path.basename(root).startswith("v"):
            continue
        for f in files:
            if f.endswith((".go", ".mod", ".sum")):
                if os.path.getmtime(os.path.join(root, f)) > bin_mtime:
                    return True
    return False


def _maybe_dev_build() -> None:
    """If ``BLENDERKIT_DEV=1``, rebuild the in-addon binary from source
    when any ``.go`` / ``go.mod`` / ``go.sum`` file is newer than it.

    Mirrors what ``bk_maya/dev.py build`` does, but only for the current
    platform and only when needed. The output overwrites the bundled
    binary under ``client/vX.Y.Z/<name>`` so the normal install/copy path
    picks it up.
    """
    if os.environ.get("BLENDERKIT_DEV", "0") != "1":
        return

    binary_path = _inplace_binary_path()
    if not _sources_newer_than(binary_path):
        return

    src_dir = _client_source_dir()
    version_file = os.path.join(src_dir, "VERSION")
    try:
        with open(version_file, encoding="utf-8") as fh:
            version = fh.read().strip()
    except OSError:
        version = CLIENT_VERSION.lstrip("v")

    goos, goarch = _go_target()
    env = {**os.environ, "GOOS": goos, "GOARCH": goarch, "CGO_ENABLED": "0"}
    ldflags = f"-X main.ClientVersion={version}"

    os.makedirs(os.path.dirname(binary_path), exist_ok=True)
    log.info("BLENDERKIT_DEV=1: building client (%s/%s) → %s", goos, goarch, binary_path)
    try:
        proc = subprocess.run(
            ["go", "build", "-o", binary_path, "-ldflags", ldflags, "."],
            env=env,
            cwd=src_dir,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError:
        log.error("BLENDERKIT_DEV=1 but `go` is not on PATH; skipping rebuild.")
        return
    if proc.returncode != 0:
        log.error("Client build failed (rc=%s):\n%s", proc.returncode, proc.stderr)
        return
    # Invalidate the installed copy so _ensure_client_binary_installed re-copies.
    installed = _installed_binary_path()
    try:
        if os.path.isfile(installed):
            os.remove(installed)
    except OSError:
        pass
    log.info("Client rebuilt OK.")


def _ensure_client_binary_installed() -> str:
    """Copy the in-addon binary to the user's global dir on first run.

    Returns the path to use for spawning. Falls back to the in-addon copy
    (``_use_inplace_client = True``) if the copy fails — e.g. when the
    addon lives on a read-only volume.
    """
    global _use_inplace_client

    _maybe_dev_build()

    src = _inplace_binary_path()
    if not os.path.isfile(src):
        raise FileNotFoundError(
            f"BlenderKit client binary not found at {src}. "
            f"Run bk_maya/dev.py to build it."
        )

    if _use_inplace_client:
        return src

    dst = _installed_binary_path()
    try:
        if not os.path.isfile(dst) or os.path.getsize(dst) != os.path.getsize(src):
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            if sys.platform != "win32":
                os.chmod(dst, 0o755)
            log.info("Installed BlenderKit client to %s", dst)
        return dst
    except OSError as exc:
        log.warning(
            "Could not install client to %s (%s); using in-addon copy.", dst, exc,
        )
        _use_inplace_client = True
        return src


def _binary_path() -> str:
    """Back-compat alias; returns the path that would actually be spawned."""
    try:
        return _ensure_client_binary_installed()
    except FileNotFoundError:
        return _inplace_binary_path()


def _log_path() -> str:
    # Mirror the Blender addon: <global_dir>/client/default.log
    log_dir = os.path.join(_prefs_mod.prefs.global_dir_resolved(), "client")
    os.makedirs(log_dir, exist_ok=True)
    return os.path.join(log_dir, "default.log")


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
    binary = _ensure_client_binary_installed()

    creation_flags = 0
    if sys.platform == "win32":
        creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    p = _prefs_mod.prefs
    ssl_context = "DISABLED" if not getattr(p, "ssl_verification", True) else ""

    log_file = open(_log_path(), "ab")
    args = [
        binary,
        "--port", port,
        "--server", global_vars.SERVER,
        "--proxy_which", getattr(p, "proxy_which", "") or "",
        "--proxy_address", getattr(p, "proxy_address", "") or "",
        "--trusted_ca_certs", "",
        "--ssl_context", ssl_context,
        "--version", f"{ADDON_VERSION}.{ADDON_BUILD}",
        "--software", SOFTWARE_NAME,
        "--pid", str(_app_id),
    ]
    log.info("Spawning BlenderKit client: %s", " ".join(args))
    proc = subprocess.Popen(
        args,
        stdout=log_file,
        stderr=log_file,
        creationflags=creation_flags,
        close_fds=(sys.platform != "win32"),
    )
    log.info("BlenderKit client PID %s on port %s", proc.pid, port)
    return proc


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
    """Best-effort: ask the client to stop and reap the process.

    Only terminates the process if *we* spawned it; clients started by
    another Maya instance are left running for that instance to manage.
    """
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


def _atexit_shutdown() -> None:
    if _process is None:
        return
    try:
        shutdown()
    except Exception:
        pass


atexit.register(_atexit_shutdown)


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


# ── Proxor (.prxc) on-demand download ────────────────────────────────────────

def asset_prxc_download(
    *,
    asset_base_id: str,
    download_url:  str,
    file_path:     str,
    api_key:       str = "",
    scene_uuid:    str = "",
) -> str:
    """Schedule a ``.prxc`` proxor download. Returns the task_id immediately.

    Completion arrives on ``/report`` as a ``prxc_download`` task whose
    ``data`` carries ``assetBaseId`` + ``file_path``.
    """
    body = {
        "PREFS":            _prefs_block(api_key),
        "addon_version":    ADDON_VERSION,
        "platform_version": platform.platform(),
        "api_key":          api_key,
        "app_id":           _app_id,
        "assetBaseId":      asset_base_id,
        "download_url":     download_url,
        "file_path":        file_path,
        "scene_uuid":       scene_uuid or str(uuid.uuid4()),
    }
    url = f"{get_base_url()}/blender/asset_prxc_download"
    resp = _http_request("POST", url, body=body)
    if not isinstance(resp, dict) or "task_id" not in resp:
        raise RuntimeError(f"Unexpected /asset_prxc_download response: {resp!r}")
    return resp["task_id"]


# ── Reports ──────────────────────────────────────────────────────────────────

def get_reports(api_key: str = "") -> list[dict[str, Any]]:
    """Poll ``/report`` and return the list of task dicts.

    The client deletes finished tasks after reporting them once, so any
    given completed task is delivered exactly one time.
    """
    global _failed_reports
    url = f"{get_base_url()}/report"
    try:
        resp = _http_request(
            "GET", url, body=_minimal_report_data(api_key),
            connect_timeout=POLL_CONNECT_TIMEOUT,
            read_timeout=POLL_READ_TIMEOUT,
        )
    except (urllib.error.URLError, Exception) as exc:
        _failed_reports += 1
        log.debug("Report poll failed (%d): %s", _failed_reports, exc)
        if _failed_reports >= _RESPAWN_AFTER_FAILURES:
            _failed_reports = 0
            _try_respawn()
        return []
    _failed_reports = 0
    if not isinstance(resp, list):
        return []
    return resp


def _try_respawn() -> None:
    """Restart the client after repeated /report failures.

    Mirrors Blender's ``handle_failed_reports`` recovery path.
    """
    global _process
    log.warning(
        "Client unresponsive after %d polls; attempting respawn.",
        _RESPAWN_AFTER_FAILURES,
    )
    with _state_lock:
        if _process is not None:
            try:
                _process.terminate()
            except Exception:
                pass
            _process = None
    try:
        ensure_running()
    except Exception as exc:
        log.error("Respawn failed: %s", exc)


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
    """Map ``assetBaseId`` → callback for thumbnail download notifications.

    Also remembers the most recently delivered path per asset so a tile
    that registers *after* the client already reported the download (the
    client deletes finished tasks after one ``/report`` poll) can pick
    up the file immediately.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cbs:    dict[str, Callable[[str], None]] = {}
        self._cached: dict[str, str]                    = {}

    def register(self, asset_base_id: str, cb: Callable[[str], None]) -> None:
        """Register *cb*. If a path was already delivered for this id, fire
        *cb* immediately and drop the registration."""
        if not asset_base_id:
            return
        with self._lock:
            path = self._cached.get(asset_base_id)
            if path and os.path.exists(path):
                # Fire outside the lock
                pass
            else:
                self._cbs[asset_base_id] = cb
                return
        try:
            cb(path)
        except Exception:
            log.exception("Thumb callback raised on replay for %s", asset_base_id)

    def unregister(self, asset_base_id: str) -> None:
        with self._lock:
            self._cbs.pop(asset_base_id, None)

    def deliver(self, asset_base_id: str, path: str) -> Callable[[str], None] | None:
        """Record *path* for *asset_base_id* and return the callback (if any)
        that the dispatcher should invoke."""
        with self._lock:
            self._cached[asset_base_id] = path
            return self._cbs.pop(asset_base_id, None)

    def clear(self) -> None:
        with self._lock:
            self._cbs.clear()
            self._cached.clear()


thumb_registry = _ThumbRegistry()


class _PrxcRegistry:
    """Map ``assetBaseId`` → callback for .prxc download notifications.

    Mirrors :class:`_ThumbRegistry`: if the client already finished the
    download before the caller registered (the client deletes finished
    tasks after one ``/report`` poll), the cached path is delivered
    immediately on ``register()``.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cbs:    dict[str, Callable[[str], None]] = {}
        self._cached: dict[str, str]                    = {}

    def register(self, asset_base_id: str, cb: Callable[[str], None]) -> None:
        if not asset_base_id:
            return
        with self._lock:
            path = self._cached.get(asset_base_id)
            if not (path and os.path.exists(path)):
                self._cbs[asset_base_id] = cb
                return
        try:
            cb(path)
        except Exception:
            log.exception("Proxor callback raised on replay for %s", asset_base_id)

    def unregister(self, asset_base_id: str) -> None:
        with self._lock:
            self._cbs.pop(asset_base_id, None)

    def deliver(self, asset_base_id: str, path: str) -> Callable[[str], None] | None:
        with self._lock:
            self._cached[asset_base_id] = path
            return self._cbs.pop(asset_base_id, None)

    def clear(self) -> None:
        with self._lock:
            self._cbs.clear()
            self._cached.clear()


prxc_registry = _PrxcRegistry()


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
            cb = thumb_registry.deliver(base_id, path)
            if cb is None:
                continue
            try:
                cb(path)
            except Exception:
                log.exception("Thumbnail callback raised for %s", base_id)

        elif ttype == "prxc_download":
            if status != "finished":
                continue
            data = task.get("data") or {}
            base_id = data.get("assetBaseId") or ""
            path    = data.get("file_path")   or ""
            if not (base_id and path):
                continue
            cb = prxc_registry.deliver(base_id, path)
            if cb is None:
                continue
            try:
                cb(path)
            except Exception:
                log.exception("Proxor callback raised for %s", base_id)

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
