"""OAuth2 PKCE authentication for BlenderKit Maya plugin.

Implements the same PKCE flow as the Blender addon but without bpy:
  1. ``login()``   — opens browser, starts a local callback server, exchanges
                     code for tokens, persists them to disk.
  2. ``logout()``  — revokes the token and clears persisted credentials.
  3. ``get_api_key()`` — returns a valid access token (refreshing if needed).

Tokens are stored in ``~/Documents/maya/blenderkit_auth.json`` (Windows),
respecting OneDrive folder redirection.
"""

from __future__ import annotations

import base64
import hashlib
import http.server
import json
import logging
import os
import random
import secrets
import string
import sys
import threading
import time
import urllib.parse
import webbrowser
from typing import Any

from ..api import client as api

log = logging.getLogger(__name__)

# -------------------------------------------------------------------------
# Token storage path (mirrors maya_module.py's OneDrive-aware Documents dir)
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


# In-memory token cache
_tokens: dict[str, Any] = {}


def _load_tokens() -> dict[str, Any]:
    global _tokens
    try:
        with open(_token_file(), encoding="utf-8") as fh:
            _tokens = json.load(fh)
    except (OSError, json.JSONDecodeError):
        _tokens = {}
    return _tokens


def _save_tokens(tokens: dict[str, Any]) -> None:
    global _tokens
    _tokens = tokens
    path = _token_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(tokens, fh, indent=2)


def _clear_tokens() -> None:
    global _tokens
    _tokens = {}
    try:
        os.remove(_token_file())
    except OSError:
        pass


# -------------------------------------------------------------------------
# PKCE helpers
# -------------------------------------------------------------------------

def _generate_pkce_pair() -> tuple[str, str]:
    """Return (code_verifier, code_challenge)."""
    rand = random.SystemRandom()
    verifier = "".join(rand.choices(string.ascii_letters + string.digits, k=128))
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


# -------------------------------------------------------------------------
# Local callback server
# -------------------------------------------------------------------------

_CALLBACK_PORT = 62485
_REDIRECT_URI = f"http://localhost:{_CALLBACK_PORT}/consumer/exchange/"

_auth_code_event = threading.Event()
_received_code: str = ""
_received_state: str = ""


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *_) -> None:  # silence access log
        pass

    def do_GET(self):  # noqa: N802
        global _received_code, _received_state
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)
        _received_code = params.get("code", [""])[0]
        _received_state = params.get("state", [""])[0]

        html = (
            b"<html><body><h2>BlenderKit login complete.</h2>"
            b"<p>You can close this tab and return to Maya.</p></body></html>"
        )
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.send_header("Content-Length", str(len(html)))
        self.end_headers()
        self.wfile.write(html)
        _auth_code_event.set()


def _start_callback_server() -> http.server.HTTPServer:
    server = http.server.HTTPServer(("localhost", _CALLBACK_PORT), _CallbackHandler)
    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    return server


# -------------------------------------------------------------------------
# Public API
# -------------------------------------------------------------------------

def is_logged_in() -> bool:
    """Return True if we have a stored access token."""
    tokens = _tokens or _load_tokens()
    return bool(tokens.get("access_token"))


def get_api_key() -> str:
    """Return a valid access token, refreshing silently if near expiry."""
    tokens = _tokens or _load_tokens()
    if not tokens:
        return ""

    expires_at = tokens.get("expires_at", 0)
    # Refresh if token expires within 1 hour
    if time.time() > expires_at - 3600:
        refresh = tokens.get("refresh_token", "")
        if not refresh:
            return ""
        try:
            new_tokens = api.refresh_tokens(refresh)
            new_tokens["expires_at"] = time.time() + new_tokens.get("expires_in", 3600)
            _save_tokens(new_tokens)
            return new_tokens.get("access_token", "")
        except Exception as exc:
            log.warning("Token refresh failed: %s", exc)
            return ""

    return tokens.get("access_token", "")


def login(timeout: float = 120.0) -> bool:
    """Open the browser OAuth flow.  Blocks until the callback is received
    or *timeout* seconds pass.  Returns True on success.
    """
    global _received_code, _received_state, _auth_code_event

    _auth_code_event = threading.Event()
    _received_code = ""
    _received_state = ""

    code_verifier, code_challenge = _generate_pkce_pair()
    state = secrets.token_urlsafe(16)

    auth_url = (
        f"{api.BASE_URL}/o/authorize/"
        f"?client_id={api.CLIENT_ID}"
        f"&response_type=code"
        f"&state={state}"
        f"&code_challenge={code_challenge}"
        f"&code_challenge_method=S256"
        f"&redirect_uri={urllib.parse.quote(_REDIRECT_URI, safe='')}"
    )

    server = _start_callback_server()
    log.info("Opening browser for BlenderKit login…")
    webbrowser.open_new_tab(auth_url)

    if not _auth_code_event.wait(timeout=timeout):
        log.error("Login timed out after %s seconds.", timeout)
        server.server_close()
        return False

    server.server_close()

    if not _received_code:
        log.error("No authorisation code received.")
        return False

    if _received_state != state:
        log.error("OAuth state mismatch — possible CSRF attack.")
        return False

    try:
        tokens = api.exchange_code_for_tokens(
            _received_code, code_verifier, _REDIRECT_URI
        )
    except Exception as exc:
        log.error("Token exchange failed: %s", exc)
        return False

    tokens["expires_at"] = time.time() + tokens.get("expires_in", 3600)
    _save_tokens(tokens)
    log.info("Login successful.")
    return True


def logout() -> None:
    """Revoke stored token and clear credentials."""
    tokens = _tokens or _load_tokens()
    access = tokens.get("access_token", "")
    if access:
        api.revoke_token(access)
    _clear_tokens()
    log.info("Logged out.")
