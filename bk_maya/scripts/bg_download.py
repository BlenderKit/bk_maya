"""BlenderKit Maya â€” background Blender script.

Executed inside ``blender --background --python <this> -- <args.json>``.

It downloads a chosen .blend resolution from BlenderKit, opens it, links/appends
the asset, exports the scene to a USD (.usd) file with UsdPreviewSurface
materials + texture references, and reports progress on stdout using the
protocol consumed by :mod:`bk_maya.core.blender_runner`::

    BK_STATUS   <stage>
    BK_PROGRESS <0..1> <message>
    BK_DONE     <output-path>
    BK_ERROR    <message>

The script is intentionally self-contained â€” it does **not** import or depend
on the BlenderKit Blender addon.  It only relies on ``bpy`` and Python stdlib.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
import urllib.error
import urllib.parse
import urllib.request
import uuid

# ---------------------------------------------------------------------------
# Stdout protocol helpers
# ---------------------------------------------------------------------------


def emit(tag: str, *parts: object) -> None:
    msg = " ".join(str(p) for p in parts)
    sys.stdout.write(f"{tag} {msg}\n")
    sys.stdout.flush()


def status(s: str) -> None:
    emit("BK_STATUS", s)


def progress(frac: float, msg: str = "") -> None:
    emit("BK_PROGRESS", f"{frac:.3f}", msg)


def done(path: str) -> None:
    emit("BK_DONE", path)


def error(msg: str) -> None:
    emit("BK_ERROR", msg)


def log_line(msg: str) -> None:
    """Print a diagnostic line; the Maya-side runner forwards these as debug logs."""
    print(f"[bg_download] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Resolution mapping (matches Blender addon's resolutions.py)
# ---------------------------------------------------------------------------

_RES_TO_FILETYPE = {
    "512": "resolution_0_5K",
    "1024": "resolution_1K",
    "2048": "resolution_2K",
    "4096": "resolution_4K",
    "8192": "resolution_8K",
}
# Ordered from smallest to largest for fallback purposes.
_RES_ORDER = ["512", "1024", "2048", "4096", "8192"]


def pick_download_url(asset_data: dict, max_resolution: str) -> tuple[str, str]:
    """Return ``(download_url, file_type_chosen)`` for *max_resolution*.

    Falls back to lower resolutions, then to ``blend`` (original) if nothing
    matching is available.
    """
    files = asset_data.get("files") or []
    by_type: dict[str, dict] = {f.get("fileType", ""): f for f in files if f.get("fileType")}

    candidates: list[str] = []
    if max_resolution != "ORIGINAL" and max_resolution in _RES_TO_FILETYPE:
        # try requested + smaller, in descending order
        cutoff = _RES_ORDER.index(max_resolution)
        candidates = [_RES_TO_FILETYPE[r] for r in reversed(_RES_ORDER[: cutoff + 1])]
    # Always allow original blend as final fallback
    candidates.append("blend")

    for ft in candidates:
        f = by_type.get(ft)
        if not f:
            continue
        url = f.get("downloadUrl") or f.get("file_path") or ""
        if url:
            return url, ft

    raise RuntimeError("No downloadable file found in asset data.")


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

# Hosts on which a Bearer token is required to fetch a signed CDN URL.
# Anything else is treated as already-signed and we MUST NOT attach Bearer
# auth (CDN/S3 reject any Authorization header that wasn't in the signature).
_BK_API_HOSTS = {"blenderkit.com", "www.blenderkit.com"}

# Maya max_resolution pref → BlenderKit fileType / Go client resolution token.
_RES_TO_RESOLUTION = {
    "512": "resolution_0_5K",
    "1024": "resolution_1K",
    "2048": "resolution_2K",
    "4096": "resolution_4K",
    "8192": "resolution_8K",
    "ORIGINAL": "ORIGINAL",
}


def _is_bk_api_url(url: str) -> bool:
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return False
    return host in _BK_API_HOSTS


def _client_post(base_url: str, path: str, payload: dict, *, timeout: int = 120) -> str:
    """POST *payload* as JSON to the local BlenderKit Go client over loopback.

    The Go client performs the real external HTTPS request on our behalf, so
    Blender's Python never opens a TLS connection (its bundled SSL has no CA
    bundle on macOS, which is why direct urllib downloads fail with
    ``CERTIFICATE_VERIFY_FAILED``).  Loopback HTTP needs no certificates.
    """
    full = base_url.rstrip("/") + path
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        full,
        data=data,
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def resolve_signed_url_via_client(
    base_url: str,
    asset_data: dict,
    max_resolution: str,
    *,
    api_key: str,
    app_id: int,
    addon_version: str,
    platform_version: str,
    scene_uuid: str,
) -> str:
    """Resolve a signed CDN URL through the Go client's blocking wrapper.

    Mirrors the Blender addon: the client selects the best file for the
    requested resolution and exchanges the API ``downloadUrl`` for a signed
    CDN URL, all over a single loopback call.
    """
    resolution = _RES_TO_RESOLUTION.get(str(max_resolution), "ORIGINAL")
    payload = {
        "addon_version": addon_version,
        "platform_version": platform_version,
        "app_id": app_id,
        "resolution": resolution,
        "asset_data": {
            "name": str(asset_data.get("name") or asset_data.get("displayName") or ""),
            "id": str(asset_data.get("id") or asset_data.get("assetBaseId") or ""),
            "files": asset_data.get("files") or [],
            "assetType": str(asset_data.get("assetType") or "model"),
            "resolution": resolution,
        },
        "PREFS": {
            "api_key": api_key,
            "scene_id": scene_uuid,
            "app_id": app_id,
            "resolution": resolution,
        },
    }
    log_line(f"resolve_signed_url_via_client: resolution={resolution}")
    raw = _client_post(base_url, "/wrappers/get_download_url", payload, timeout=60)
    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"get_download_url response was not JSON: {raw[:200]!r}") from exc
    if not body.get("can_download", False):
        raise RuntimeError(f"Server reports asset is not downloadable: {body}")
    signed = body.get("download_url") or ""
    if not signed:
        raise RuntimeError(f"Client did not return a download_url: {body}")
    log_line(f"resolve_signed_url_via_client: signed={signed[:120]}…")
    return signed


def download_file_via_client(base_url: str, url: str, dest_path: str, *, app_id: int) -> None:
    """Download *url* to *dest_path* via the Go client's blocking wrapper.

    The signed CDN URL carries its own auth token, so we pass an empty API key
    (the client then sends no ``Authorization`` header, matching its own
    ``downloadAsset`` path).
    """
    os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
    status("Downloading")
    payload = {"app_id": app_id, "api_key": "", "url": url, "filepath": dest_path}
    log_line(f"download_file_via_client: GET {url[:160]}… → {dest_path}")
    _client_post(base_url, "/wrappers/blocking_file_download", payload, timeout=900)
    progress(0.70, "Downloaded")


def resolve_signed_url(api_url: str, *, api_key: str, scene_uuid: str) -> str:
    """Exchange a BlenderKit API ``downloadUrl`` for a signed CDN URL.

    BlenderKit's search response returns ``downloadUrl`` values pointing at an
    API endpoint (e.g. ``https://www.blenderkit.com/api/v1/download/<id>/``).
    Hitting it with ``Bearer`` auth + ``scene_uuid`` returns JSON like
    ``{"filePath": "https://public.blenderkit.com/.../file.blend?verify=..."}``.

    If *api_url* is already on a non-blenderkit.com host (i.e. CDN/S3), it's
    returned unchanged.
    """
    if not _is_bk_api_url(api_url):
        log_line(f"resolve_signed_url: skipping non-API host: {api_url}")
        return api_url

    if not api_key:
        raise RuntimeError(
            "Authentication required for signed download URL â€” please log in to BlenderKit in Maya first."
        )

    sep = "&" if "?" in api_url else "?"
    full = f"{api_url}{sep}scene_uuid={urllib.parse.quote(scene_uuid)}"
    headers = {
        "User-Agent": "BlenderKit-Maya/0.1",
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
    }
    log_line(f"resolve_signed_url: GET {full}")
    req = urllib.request.Request(full, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            ctype = resp.headers.get("Content-Type", "")
            log_line(f"resolve_signed_url: status={resp.status} content-type={ctype} body-prefix={raw[:200]!r}")
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        raise RuntimeError(f"signed URL request returned HTTP {exc.code} {exc.reason} â€” body: {body}") from exc

    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"signed URL response was not JSON: {raw[:200]!r}") from exc

    signed = body.get("filePath") or ""
    if not signed:
        raise RuntimeError(f"Server did not return filePath: {body}")
    log_line(f"resolve_signed_url: signed={signed[:120]}â€¦")
    return signed


def download_file(url: str, dest_path: str, *, api_key: str = "") -> None:
    """Download *url* to *dest_path* and emit BK_PROGRESS lines.

    Signed CDN URLs include their own auth token in the query string, so we
    must **not** send a ``Bearer`` header (it would be rejected as 400/403).
    The Go client also explicitly clears the API key for the file fetch
    (``client/download.go`` ``downloadAsset`` passes ``apiKey=""``).
    """
    headers = {
        "User-Agent": "BlenderKit-Maya/0.1",
        # Mirror the Go client â€” allow compressed transfer (#1486).
        "Cookie": "allow_compression=true",
    }
    if api_key and _is_bk_api_url(url):
        # Only attach Bearer for blenderkit.com API hosts (not CDN/S3).
        headers["Authorization"] = f"Bearer {api_key}"

    log_line(f"download_file: GET {url[:160]}â€¦")
    req = urllib.request.Request(url, headers=headers)

    status("Downloading")
    try:
        resp_cm = urllib.request.urlopen(req, timeout=60)
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        log_line(f"download_file: HTTPError body: {body}")
        raise

    with resp_cm as resp:
        total = int(resp.headers.get("Content-Length") or 0)
        os.makedirs(os.path.dirname(dest_path) or ".", exist_ok=True)
        written = 0
        chunk_size = 64 * 1024
        with open(dest_path, "wb") as fh:
            while True:
                chunk = resp.read(chunk_size)
                if not chunk:
                    break
                fh.write(chunk)
                written += len(chunk)
                if total:
                    progress(written / total * 0.70, "Downloading")
    progress(0.70, "Downloaded")


# ---------------------------------------------------------------------------
# Delegate Blend → USD export to client/tools/export_usd.py
# ---------------------------------------------------------------------------


def _find_export_usd_script(args: dict) -> str:
    """Locate ``export_usd.py`` shipped under ``client/tools/``.

    Lookup order:
      1. ``args["export_usd_script"]`` (explicit override from Maya side).
      2. ``$BLENDERKIT_TOOLS_DIR/export_usd.py``.
      3. Walk up from this script: ``<repo>/client/tools/export_usd.py``.
    """
    candidates: list[str] = []
    override = args.get("export_usd_script") or ""
    if override:
        candidates.append(override)

    env_dir = os.environ.get("BLENDERKIT_TOOLS_DIR", "")
    if env_dir:
        candidates.append(os.path.join(env_dir, "export_usd.py"))

    here = os.path.dirname(os.path.abspath(__file__))
    cur = here
    for _ in range(6):
        cur = os.path.dirname(cur)
        if not cur:
            break
        candidates.append(os.path.join(cur, "client", "tools", "export_usd.py"))

    for c in candidates:
        if c and os.path.isfile(c):
            return c
    raise RuntimeError(
        f"export_usd.py not found. Tried: {candidates}. Set BLENDERKIT_TOOLS_DIR or pass export_usd_script in args."
    )


def run_export_usd(args: dict, blend_path: str, out_usd: str) -> None:
    """Exec ``client/tools/export_usd.py`` in this Blender process.

    Reuses the same interpreter (no extra subprocess) by setting up the
    recipe's expected argv and ``exec``ing it as ``__main__``. Its stdout
    BK_* protocol lines flow straight back to the Maya-side runner because
    we share ``sys.stdout``.
    """
    script_path = _find_export_usd_script(args)
    log_line(f"delegating USD export to: {script_path}")

    # Build params JSON next to the .blend so the recipe can pick it up.
    params = {
        "blend_path": blend_path,
        "out_usd": out_usd,
        "max_resolution": args.get("max_resolution", ""),
    }
    params_path = os.path.join(os.path.dirname(blend_path) or ".", "_export_usd_params.json")
    with open(params_path, "w", encoding="utf-8") as fh:
        json.dump(params, fh)

    saved_argv = sys.argv
    try:
        # Recipe ABI: <script> -- <params.json>
        sys.argv = [script_path, "--", params_path]
        ns: dict = {"__name__": "__main__", "__file__": script_path}
        with open(script_path, encoding="utf-8") as fh:
            code = compile(fh.read(), script_path, "exec")
        try:
            exec(code, ns)  # noqa: S102 - running a trusted recipe script in-process
        except SystemExit as ex:
            if ex.code not in (None, 0):
                raise RuntimeError(f"export_usd.py exited with code {ex.code}") from ex
    finally:
        sys.argv = saved_argv
        try:
            os.remove(params_path)
        except OSError:
            pass


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def parse_args() -> dict:
    if "--" not in sys.argv:
        raise RuntimeError("No '--' separator found in argv.")
    args = sys.argv[sys.argv.index("--") + 1 :]
    if not args:
        raise RuntimeError("Missing args JSON path.")
    with open(args[0], encoding="utf-8") as fh:
        return json.load(fh)


def main() -> int:
    try:
        args = parse_args()
    except Exception as exc:
        error(f"argument parsing failed: {exc}")
        return 2

    asset_data = args.get("asset_data") or {}
    max_resolution = args.get("max_resolution", "2048")
    # Accept either ``out_usd`` (new) or ``out_glb`` (legacy) so we don't
    # break older Maya-side callers during the switchover.
    out_usd = args.get("out_usd") or args.get("out_glb", "")
    api_key = args.get("api_key", "")
    work_dir = args.get("work_dir") or os.path.dirname(out_usd) or "."
    # Maya-side computes the exact cache path so re-drops hit the existing
    # .blend instead of triggering a re-download.
    blend_path = args.get("blend_path") or ""

    # Networking goes through the local BlenderKit Go client over loopback;
    # direct HTTPS from Blender fails SSL verification on macOS.
    client_base_url = args.get("client_base_url") or ""
    app_id = int(args.get("app_id") or 0)
    addon_version = args.get("addon_version") or "0.0.0"
    platform_version = args.get("platform_version") or ""

    progress(0.0, "starting")

    if blend_path and os.path.isfile(blend_path) and os.path.getsize(blend_path) > 0:
        status("Using cached .blend")
        log_line(f"reusing cached blend: {blend_path}")
        progress(0.70, "Using cached .blend")
    else:
        scene_uuid = asset_data.get("sceneUuid") or asset_data.get("scene_uuid") or str(uuid.uuid4())
        if not blend_path:
            blend_path = os.path.join(work_dir, f"_bk_dl_{asset_data.get('id', 'asset')}.blend")

        if client_base_url:
            # Preferred path: resolve + download through the Go client.
            try:
                signed_url = resolve_signed_url_via_client(
                    client_base_url,
                    asset_data,
                    max_resolution,
                    api_key=api_key,
                    app_id=app_id,
                    addon_version=addon_version,
                    platform_version=platform_version,
                    scene_uuid=scene_uuid,
                )
            except Exception as exc:
                error(f"signed URL request failed: {exc}")
                traceback.print_exc(file=sys.stdout)
                return 1
            try:
                download_file_via_client(client_base_url, signed_url, blend_path, app_id=app_id)
            except Exception as exc:
                error(f"download failed: {exc}")
                traceback.print_exc(file=sys.stdout)
                return 1
        else:
            # Legacy fallback (no client URL supplied): direct HTTPS.
            try:
                url, ft = pick_download_url(asset_data, max_resolution)
                status(f"picked {ft}")
                log_line(f"picked file type={ft} url={url}")
            except Exception as exc:
                error(str(exc))
                return 1
            try:
                signed_url = resolve_signed_url(url, api_key=api_key, scene_uuid=scene_uuid)
            except urllib.error.HTTPError as exc:
                error(f"HTTP {exc.code} while requesting signed URL: {exc.reason}")
                return 1
            except Exception as exc:
                error(f"signed URL request failed: {exc}")
                traceback.print_exc(file=sys.stdout)
                return 1
            try:
                download_file(signed_url, blend_path, api_key=api_key)
            except urllib.error.HTTPError as exc:
                error(f"HTTP {exc.code} while downloading: {exc.reason}")
                return 1
            except Exception as exc:
                error(f"download failed: {exc}")
                traceback.print_exc(file=sys.stdout)
                return 1

    try:
        if os.path.isfile(out_usd) and os.path.getsize(out_usd) > 0:
            status("Using cached USD")
            log_line(f"reusing cached usd: {out_usd}")
            progress(0.99, "Using cached USD")
        else:
            run_export_usd(args, blend_path, out_usd)
    except Exception as exc:
        error(f"export failed: {exc}")
        traceback.print_exc(file=sys.stdout)
        return 1

    progress(1.0, "done")
    done(out_usd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
