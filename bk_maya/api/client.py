"""BlenderKit REST API client.

Talks directly to https://www.blenderkit.com/api/v1/.
All network calls are synchronous; callers are expected to run them on a
worker thread so the Maya UI thread is never blocked.
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

log = logging.getLogger(__name__)

BASE_URL = "https://www.blenderkit.com"
API_V1 = f"{BASE_URL}/api/v1"

# -------------------------------------------------------------------------
# Internal helpers
# -------------------------------------------------------------------------

_DEFAULT_HEADERS = {
    "User-Agent": "BlenderKit-Maya/0.1",
    "Accept": "application/json",
}


def _request(
    method: str,
    url: str,
    *,
    params: dict[str, Any] | None = None,
    data: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    api_key: str = "",
    timeout: float = 15.0,
) -> dict[str, Any]:
    """Execute an HTTP request and return the parsed JSON body.

    Raises ``urllib.error.HTTPError`` on 4xx/5xx responses.
    """
    all_headers = dict(_DEFAULT_HEADERS)
    if api_key:
        all_headers["Authorization"] = f"Bearer {api_key}"
    if headers:
        all_headers.update(headers)

    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"

    body: bytes | None = None
    if data is not None:
        encoded = urllib.parse.urlencode(data).encode()
        body = encoded
        all_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")

    req = urllib.request.Request(url, data=body, headers=all_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        body_text = exc.read().decode(errors="replace")
        log.error("HTTP %s %s → %d: %s", method, url, exc.code, body_text)
        raise


# -------------------------------------------------------------------------
# Auth endpoints
# -------------------------------------------------------------------------

CLIENT_ID = "IdFRwa3SGA8eMpzhRVFMg5Ts8sPK93xBjif93x0F"
TOKEN_URL = f"{BASE_URL}/o/token/"
REVOKE_URL = f"{BASE_URL}/o/revoke-token/"


def exchange_code_for_tokens(
    code: str, code_verifier: str, redirect_uri: str
) -> dict[str, Any]:
    """Exchange an OAuth2 authorisation code for access + refresh tokens."""
    return _request(
        "POST",
        TOKEN_URL,
        data={
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": redirect_uri,
        },
    )


def refresh_tokens(refresh_token: str) -> dict[str, Any]:
    """Use a refresh token to obtain a new access token."""
    return _request(
        "POST",
        TOKEN_URL,
        data={
            "grant_type": "refresh_token",
            "client_id": CLIENT_ID,
            "refresh_token": refresh_token,
        },
    )


def revoke_token(token: str) -> None:
    """Revoke an access or refresh token (best-effort; errors are logged)."""
    try:
        _request(
            "POST",
            REVOKE_URL,
            data={"client_id": CLIENT_ID, "token": token},
        )
    except Exception as exc:
        log.warning("Token revocation failed (ignoring): %s", exc)


def get_profile(api_key: str) -> dict[str, Any]:
    """Return the authenticated user's profile."""
    return _request("GET", f"{API_V1}/me/", api_key=api_key)


# -------------------------------------------------------------------------
# Search endpoint
# -------------------------------------------------------------------------

SEARCH_URL = f"{API_V1}/search/"

ASSET_TYPES = ("model", "material", "scene", "hdr", "brush", "printable")


def build_search_url(
    query: str = "",
    asset_type: str = "model",
    order: str = "",
    page_size: int = 24,
    extra_params: dict[str, Any] | None = None,
    next_url: str = "",
) -> str:
    """Return the full ``https://www.blenderkit.com/api/v1/search/?...`` URL.

    When *next_url* is supplied (cursor pagination) it is returned verbatim.
    The Go client expects this URL as the ``urlquery`` field of an
    ``/blender/asset_search`` request and GETs it directly.
    """
    if next_url:
        return next_url
    if asset_type not in ASSET_TYPES:
        raise ValueError(f"asset_type must be one of {ASSET_TYPES}")

    free_first = False
    filter_params: dict[str, Any] = {}
    if extra_params:
        for k, v in extra_params.items():
            if k == "is_free":
                free_first = bool(v) and str(v).lower() not in ("false", "0", "")
            else:
                filter_params[k] = v

    if order:
        effective_order = order
    elif not query:
        effective_order = "-last_blend_upload,-last_zip_file_upload"
    else:
        effective_order = "_score"

    if free_first:
        effective_order = "-is_free," + effective_order

    q_tokens: list[str] = []
    if query:
        q_tokens.append(urllib.parse.quote_plus(query))
    q_tokens.append(f"asset_type:{asset_type}")
    q_tokens.append("sexualizedContent:")
    q_tokens.append(f"order:{effective_order}")
    for k, v in filter_params.items():
        q_tokens.append(f"{k}:{urllib.parse.quote_plus(str(v))}")

    query_str = "+".join(q_tokens)
    if not query:
        query_str = "+" + query_str

    other: dict[str, Any] = {
        "dict_parameters": 1,
        "page_size": page_size,
        "addon_version": "0.1.0",
        "addon_type": "maya",
    }
    return f"{SEARCH_URL}?query={query_str}&{urllib.parse.urlencode(other)}"


def search(
    query: str = "",
    asset_type: str = "model",
    order: str = "",           # empty → auto-determined from query content
    page_size: int = 24,
    page_offset: int = 0,
    api_key: str = "",
    extra_params: dict[str, Any] | None = None,
    next_url: str = "",        # if set, use this cursor URL directly (pagination)
) -> dict[str, Any]:
    """Search BlenderKit assets.

    The BlenderKit API uses cursor-based pagination: each response contains
    a ``next`` URL that must be followed for subsequent pages.  When
    *next_url* is provided (from a previous response), it is used verbatim
    and all other parameters are ignored.

    Returns the raw JSON response dict (``results``, ``count``, ``next``, …).
    """
    # Fast path: cursor pagination — just follow the server-provided URL.
    if next_url:
        return _request("GET", next_url, api_key=api_key)
    if asset_type not in ASSET_TYPES:
        raise ValueError(f"asset_type must be one of {ASSET_TYPES}")

    # --- Pull "free only" out of extra_params (it's an ordering hint, not a
    # hard filter, in Blender): prepend "-is_free" to the order list. ---------
    free_first = False
    filter_params: dict[str, Any] = {}
    if extra_params:
        for k, v in extra_params.items():
            if k == "is_free":
                # Truthy → free assets first in ordering
                free_first = bool(v) and str(v).lower() not in ("false", "0", "")
            else:
                filter_params[k] = v

    # --- Determine effective sort order (mirrors Blender's decide_ordering) ---
    if order:
        effective_order = order
    elif not query:
        # Empty search (default/startup): newest uploads first
        effective_order = "-last_blend_upload,-last_zip_file_upload"
    else:
        # Keyword search: full-text relevance
        effective_order = "_score"

    if free_first:
        effective_order = "-is_free," + effective_order

    # --- Build the query string in BlenderKit's embedded format --------------
    # Format: [user_keywords +]asset_type:X +sexualizedContent: +order:Y
    #         +filter_key:filter_value (one per extra param, matching Blender)
    # The leading '+' before each token is a literal '+' in the URL (= space
    # when decoded), which the server uses as a token separator.
    q_tokens: list[str] = []
    if query:
        q_tokens.append(urllib.parse.quote_plus(query))
    q_tokens.append(f"asset_type:{asset_type}")
    q_tokens.append("sexualizedContent:")   # empty value → exclude NSFW
    q_tokens.append(f"order:{effective_order}")

    # Embed remaining filter params inside the query token (Blender-style).
    for k, v in filter_params.items():
        q_tokens.append(f"{k}:{urllib.parse.quote_plus(str(v))}")

    # Join with '+' separator; add a leading '+' when there are no keywords
    # so the first token also has the expected leading separator.
    query_str = "+".join(q_tokens)
    if not query:
        query_str = "+" + query_str

    # --- Other query-string parameters (NOT embedded in query token) ---------
    other: dict[str, Any] = {
        "dict_parameters": 1,
        "page_size": page_size,
        "addon_version": "0.1.0",
        "addon_type": "maya",
    }

    url = f"{SEARCH_URL}?query={query_str}&{urllib.parse.urlencode(other)}"
    return _request("GET", url, api_key=api_key)


# -------------------------------------------------------------------------
# Thumbnail download
# -------------------------------------------------------------------------

def download_thumbnail(url: str, dest_path: str, *, timeout: float = 10.0) -> str:
    """Download a thumbnail image to *dest_path*.  Returns *dest_path*."""
    req = urllib.request.Request(url, headers={"User-Agent": _DEFAULT_HEADERS["User-Agent"]})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    with open(dest_path, "wb") as fh:
        fh.write(data)
    return dest_path
