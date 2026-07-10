"""Check GitHub Releases for a newer Blendkit Maya plugin version.

The plugin ships from the ``BlenderKit/bk_maya`` GitHub repository:

  * **stable** releases are tagged ``v<major>.<minor>.<YYMMDDHHmm>`` and are
    published as normal (non pre-release) releases.  The latest one is exposed
    by the ``/releases/latest`` endpoint.
  * **alpha** builds are tagged the same way but with a ``-alpha`` suffix
    (``v<major>.<minor>.<YYMMDDHHmm>-alpha``) and flagged as GitHub
    pre-releases. The newest one is found by listing recent releases.

By default :func:`check_for_update` only looks at the newest *stable* release.
When ``include_alpha`` is enabled (mirrors ``prefs.include_alpha_updates``) it
also considers the newest alpha build and reports whichever is newer.

All calls are synchronous and network-bound — run them on a worker thread so
Maya's UI stays responsive (see :func:`check_for_update_async`).
"""

from __future__ import annotations

import dataclasses
import json
import logging
import re
import ssl
import threading
import urllib.error
import urllib.request
from collections.abc import Callable

from .. import _version
from .prefs import prefs

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

GITHUB_REPO = "BlenderKit/bk_maya"
"""``owner/name`` of the repository that publishes plugin releases."""

_API_LATEST = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
_API_LIST = f"https://api.github.com/repos/{GITHUB_REPO}/releases?per_page=30"

RELEASES_PAGE = f"https://github.com/{GITHUB_REPO}/releases"
"""Human-facing releases page (shown to the user when an update is found)."""

_REQUEST_TIMEOUT = 10.0

# major.minor.<timestamp>  with an optional  -alpha  suffix.
_VERSION_RE = re.compile(r"\d+\.\d+\.\d{4,}(?:-alpha)?")


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class ReleaseInfo:
    """A single published release relevant to update checks."""

    version: str
    """Full version string, e.g. ``0.1.2506071430`` or ``0.1.2601011200-alpha``."""

    tag: str
    """Git tag the release points at (``v0.1.2506071430`` or ``v0.1.…-alpha``)."""

    prerelease: bool
    """True for alpha (pre-release) builds."""

    html_url: str
    """GitHub release page URL."""


@dataclasses.dataclass(frozen=True)
class UpdateResult:
    """Outcome of an update check."""

    current_version: str
    """The version currently running."""

    latest: ReleaseInfo | None
    """Newest applicable release, or ``None`` if none could be determined."""

    update_available: bool
    """True when :attr:`latest` is strictly newer than the running version."""

    checked_alpha: bool
    """Whether the alpha channel was included in this check."""


# ---------------------------------------------------------------------------
# Version comparison
# ---------------------------------------------------------------------------


def _parse(version: str):
    """Parse a version string with ``packaging`` for a robust ordering.

    The timestamp segment (``YYMMDDHHmm``) dominates the ordering, so alpha and
    stable builds compare sensibly across channels. Returns ``None`` when the
    string cannot be parsed.
    """
    try:
        from packaging.version import InvalidVersion, Version
    except Exception:  # pragma: no cover - packaging is always shipped
        log.debug("packaging unavailable; cannot compare versions")
        return None
    try:
        return Version(version)
    except InvalidVersion:
        return None


def _is_newer(candidate: str, current: str) -> bool:
    """True when *candidate* is a strictly newer version than *current*."""
    c_new = _parse(candidate)
    c_cur = _parse(current)
    if c_new is None or c_cur is None:
        return False
    return c_new > c_cur


def _extract_version(release: dict) -> str | None:
    """Pull a full version string out of a GitHub release payload.

    Both channels tag as ``v<version>`` — stable ``v0.1.2506071430`` and alpha
    ``v0.1.2607101129-alpha`` — so ``tag_name`` normally carries it. We fall
    back to the release name/body just in case.
    """
    for field in ("tag_name", "name", "body"):
        text = release.get(field) or ""
        match = _VERSION_RE.search(text)
        if match:
            return match.group(0)
    return None


# ---------------------------------------------------------------------------
# Networking
# ---------------------------------------------------------------------------


def _fetch_release(url: str) -> dict | None:
    """GET a GitHub release JSON payload, or ``None`` on any failure."""
    payload = _fetch_json(url)
    return payload if isinstance(payload, dict) else None


def _fetch_json(url: str):
    """GET and decode a GitHub JSON payload (object or list), or ``None``."""
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"bk_maya/{_version.get_version()}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    context: ssl.SSLContext | None = None
    if not prefs.ssl_verification:
        context = ssl.create_default_context()
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE

    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=_REQUEST_TIMEOUT, context=context) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        # 404 simply means that resource has no release yet (e.g. no stable).
        if exc.code == 404:
            log.debug("No release found at %s (404)", url)
        else:
            log.warning("Update check failed (%s): HTTP %s", url, exc.code)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log.warning("Update check failed (%s): %s", url, exc)
    except (ValueError, json.JSONDecodeError) as exc:
        log.warning("Update check returned invalid JSON (%s): %s", url, exc)
    return None


def _to_release_info(payload: dict, *, expect_prerelease: bool) -> ReleaseInfo | None:
    """Adapt a GitHub release payload into a :class:`ReleaseInfo`."""
    version = _extract_version(payload)
    if not version:
        log.debug("Could not extract version from release %r", payload.get("tag_name"))
        return None
    return ReleaseInfo(
        version=version,
        tag=payload.get("tag_name", ""),
        prerelease=bool(payload.get("prerelease", expect_prerelease)),
        html_url=payload.get("html_url", RELEASES_PAGE),
    )


def fetch_latest_stable() -> ReleaseInfo | None:
    """Return the newest published *stable* release, or ``None``."""
    payload = _fetch_release(_API_LATEST)
    if not payload:
        return None
    return _to_release_info(payload, expect_prerelease=False)


def fetch_latest_alpha() -> ReleaseInfo | None:
    """Return the newest *alpha* (pre-release) build, or ``None``.

    Alpha builds are versioned pre-releases tagged ``v<version>-alpha``, so we
    list recent releases and pick the newest one flagged as a pre-release.
    """
    payload = _fetch_json(_API_LIST)
    if not isinstance(payload, list):
        return None
    latest: ReleaseInfo | None = None
    for release in payload:
        if not isinstance(release, dict) or not release.get("prerelease"):
            continue
        info = _to_release_info(release, expect_prerelease=True)
        if info and (latest is None or _is_newer(info.version, latest.version)):
            latest = info
    return latest


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_for_update(include_alpha: bool | None = None) -> UpdateResult:
    """Check GitHub for a newer plugin release.

    Args:
        include_alpha: When ``True`` also consider the newest alpha (pre-release)
            build and report whichever channel is newer. When ``None`` (default)
            the value is taken from ``prefs.include_alpha_updates``.

    Returns:
        An :class:`UpdateResult` describing the newest applicable release and
        whether it is newer than the running version. Network failures yield a
        result with ``update_available=False`` and ``latest=None`` rather than
        raising.
    """
    if include_alpha is None:
        include_alpha = prefs.include_alpha_updates

    current = _version.get_version()

    candidates: list[ReleaseInfo] = []
    stable = fetch_latest_stable()
    if stable:
        candidates.append(stable)
    if include_alpha:
        alpha = fetch_latest_alpha()
        if alpha:
            candidates.append(alpha)

    latest: ReleaseInfo | None = None
    for rel in candidates:
        if latest is None or _is_newer(rel.version, latest.version):
            latest = rel

    available = bool(latest and _is_newer(latest.version, current))
    return UpdateResult(
        current_version=current,
        latest=latest,
        update_available=available,
        checked_alpha=include_alpha,
    )


def check_for_update_async(
    callback: Callable[[UpdateResult], None],
    include_alpha: bool | None = None,
) -> threading.Thread:
    """Run :func:`check_for_update` on a daemon thread and invoke *callback*.

    The callback receives the :class:`UpdateResult` on the worker thread — if it
    touches Qt/Maya UI it must marshal back to the main thread itself. Returns
    the started thread so callers can join it if needed (e.g. in tests).
    """

    def _run() -> None:
        try:
            result = check_for_update(include_alpha)
        except Exception:  # never let a background check crash silently-with-traceback
            log.exception("Unexpected error during update check")
            return
        try:
            callback(result)
        except Exception:
            log.exception("Update-check callback raised")

    thread = threading.Thread(target=_run, name="bk-update-check", daemon=True)
    thread.start()
    return thread
