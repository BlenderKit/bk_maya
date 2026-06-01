"""OAuth2 PKCE authentication for the BlenderKit Maya plugin.

Mirrors the Blender addon flow: the local ``blenderkit-client`` Go process
owns the OAuth callback on ``http://localhost:{port}/consumer/exchange/`` and
delivers tokens back through ``/report`` as a ``login`` task. This module
just generates the PKCE pair, hands the verifier to the client, opens the
browser, and waits for the login task on a callback.

Tokens are persisted to ``~/Documents/maya/blenderkit_auth.json`` so they
survive Maya restarts.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import random
import secrets
import string
import sys
import threading
import time
import webbrowser
from typing import Any

from ..api import client as api
from . import client_lib

log = logging.getLogger(__name__)

# How early before expiry to start considering a token stale (seconds).
_REFRESH_RESERVE = 3 * 24 * 3600  # 3 days, mirrors Blender addon

# -------------------------------------------------------------------------
# Token storage
# -------------------------------------------------------------------------

def _token_file() -> str:
    if sys.platform == "win32":
        import ctypes
        buf = ctypes.create_unicode_buffer(260)
        ctypes.windll.shell32.SHGetFolderPathW(None, 0x0005, None, 0, buf)
        docs = buf.value
    elif sys.platform == "darwin":
        docs = os.path.expanduser("~/Library/Preferences/Autodesk/maya")
    else:
        docs = os.path.expanduser("~/maya")
    return os.path.join(docs, "maya", "blenderkit_auth.json")


_tokens: dict[str, Any] = {}
_tokens_lock = threading.Lock()


def _load_tokens() -> dict[str, Any]:
    global _tokens
    with _tokens_lock:
        if _tokens:
            return dict(_tokens)
        try:
            with open(_token_file(), encoding="utf-8") as fh:
                _tokens = json.load(fh)
        except (OSError, json.JSONDecodeError):
            _tokens = {}
        return dict(_tokens)


def _save_tokens(tokens: dict[str, Any]) -> None:
    global _tokens
    with _tokens_lock:
        _tokens = dict(tokens)
        path = _token_file()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(_tokens, fh, indent=2)


def _clear_tokens() -> None:
    global _tokens
    with _tokens_lock:
        _tokens = {}
        try:
            os.remove(_token_file())
        except OSError:
            pass


# -------------------------------------------------------------------------
# PKCE
# -------------------------------------------------------------------------

def _generate_pkce_pair() -> tuple[str, str]:
    rand = random.SystemRandom()
    verifier = "".join(rand.choices(string.ascii_letters + string.digits, k=128))
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


# -------------------------------------------------------------------------
# Login task plumbing
# -------------------------------------------------------------------------

_login_event = threading.Event()
_login_error: str = ""

# Refresh suppression so concurrent get_api_key() calls only fire one /refresh.
_refresh_inflight = False
_refresh_lock = threading.Lock()


def _on_login_task(result: dict[str, Any], status: str, message: str) -> None:
    """Callback registered with the client for every ``login`` task."""
    global _login_error, _refresh_inflight
    if status == "finished" and result.get("access_token"):
        tokens = {
            "access_token":  result["access_token"],
            "refresh_token": result.get("refresh_token", ""),
            "expires_in":    result.get("expires_in", 3600),
            "expires_at":    time.time() + int(result.get("expires_in", 3600)),
        }
        _save_tokens(tokens)
        log.info("BlenderKit tokens received from client.")
    else:
        _login_error = message or "Login failed"
        log.error("Login task failed: %s", _login_error)

    with _refresh_lock:
        _refresh_inflight = False
    _login_event.set()


# Register the login callback once on import so refreshes happening before
# any explicit login() call are still persisted.
client_lib.set_login_callback(_on_login_task)


# -------------------------------------------------------------------------
# Public API
# -------------------------------------------------------------------------

def is_logged_in() -> bool:
    return bool(_load_tokens().get("access_token"))


def get_api_key() -> str:
    """Return the cached access token. Triggers a background refresh if the
    token is within ``_REFRESH_RESERVE`` seconds of expiry, but never blocks.
    """
    tokens = _load_tokens()
    access  = tokens.get("access_token",  "")
    refresh = tokens.get("refresh_token", "")
    if not access:
        return ""

    expires_at = float(tokens.get("expires_at", 0))
    if refresh and time.time() + _REFRESH_RESERVE >= expires_at:
        _request_refresh(refresh, access)

    return access


def _request_refresh(refresh: str, old_api_key: str) -> None:
    """Fire-and-forget refresh via the client (deduped)."""
    global _refresh_inflight
    with _refresh_lock:
        if _refresh_inflight:
            return
        _refresh_inflight = True
    try:
        client_lib.ensure_running()
        client_lib.refresh_token(refresh, old_api_key)
        log.info("Token refresh requested via client.")
    except Exception as exc:
        with _refresh_lock:
            _refresh_inflight = False
        log.warning("Token refresh request failed: %s", exc)


def login(timeout: float = 180.0) -> bool:
    """Open the browser OAuth flow. Blocks until the client reports a login
    task or *timeout* elapses. Returns True on success.
    """
    global _login_error

    client_lib.ensure_running()
    port = client_lib._active_port  # noqa: SLF001
    if not port:
        log.error("Client not running; cannot start login.")
        return False

    verifier, challenge = _generate_pkce_pair()
    state = secrets.token_urlsafe(16)

    try:
        client_lib.send_oauth_verification_data(verifier, state)
    except Exception as exc:
        log.error("Failed to send PKCE verifier to client: %s", exc)
        return False

    redirect_uri = f"http://localhost:{port}/consumer/exchange/"
    auth_url = (
        f"{api.BASE_URL}/o/authorize"
        f"?client_id={client_lib.OAUTH_CLIENT_ID}"
        f"&response_type=code"
        f"&state={state}"
        f"&redirect_uri={redirect_uri}"
        f"&code_challenge={challenge}"
        f"&code_challenge_method=S256"
    )

    _login_event.clear()
    _login_error = ""
    log.info("Opening browser for BlenderKit login (callback on port %s)…", port)
    webbrowser.open_new_tab(auth_url)

    if not _login_event.wait(timeout=timeout):
        log.error("Login timed out after %s seconds.", timeout)
        return False

    if _login_error:
        return False
    return is_logged_in()


def logout() -> None:
    """Revoke tokens on the server and clear local storage."""
    tokens = _load_tokens()
    refresh = tokens.get("refresh_token", "")
    access  = tokens.get("access_token", "")
    if refresh:
        try:
            client_lib.ensure_running()
            client_lib.oauth2_logout(refresh, access)
        except Exception as exc:
            log.warning("Client-side logout failed: %s", exc)
    _clear_tokens()
    log.info("Logged out.")
