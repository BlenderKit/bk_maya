"""BlenderKit Maya — background Blender script.

Executed inside ``blender --background --python <this> -- <args.json>``.

It downloads a chosen .blend resolution from BlenderKit, opens it, links/appends
the asset, exports the scene to a USD (.usd) file with UsdPreviewSurface
materials + texture references, and reports progress on stdout using the
protocol consumed by :mod:`bk_maya.core.blender_runner`::

    BK_STATUS   <stage>
    BK_PROGRESS <0..1> <message>
    BK_DONE     <output-path>
    BK_ERROR    <message>

The script is intentionally self-contained — it does **not** import or depend
on the BlenderKit Blender addon.  It only relies on ``bpy`` and Python stdlib.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
import urllib.parse
import urllib.request
import urllib.error
import uuid

import bpy  # type: ignore[import-not-found]


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
    "512":  "resolution_0_5K",
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
        candidates = [_RES_TO_FILETYPE[r] for r in reversed(_RES_ORDER[:cutoff + 1])]
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


def _is_bk_api_url(url: str) -> bool:
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return False
    return host in _BK_API_HOSTS


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
            "Authentication required for signed download URL — please log in "
            "to BlenderKit in Maya first."
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
            log_line(
                f"resolve_signed_url: status={resp.status} content-type={ctype} "
                f"body-prefix={raw[:200]!r}"
            )
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            pass
        raise RuntimeError(
            f"signed URL request returned HTTP {exc.code} {exc.reason} — body: {body}"
        ) from exc

    try:
        body = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            f"signed URL response was not JSON: {raw[:200]!r}"
        ) from exc

    signed = body.get("filePath") or ""
    if not signed:
        raise RuntimeError(f"Server did not return filePath: {body}")
    log_line(f"resolve_signed_url: signed={signed[:120]}…")
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
        # Mirror the Go client — allow compressed transfer (#1486).
        "Cookie": "allow_compression=true",
    }
    if api_key and _is_bk_api_url(url):
        # Only attach Bearer for blenderkit.com API hosts (not CDN/S3).
        headers["Authorization"] = f"Bearer {api_key}"

    log_line(f"download_file: GET {url[:160]}…")
    req = urllib.request.Request(url, headers=headers)

    status("downloading")
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
                    progress(written / total * 0.70, f"downloaded {written}/{total}")
    progress(0.70, "downloaded")


# ---------------------------------------------------------------------------
# Blend → USD export
# ---------------------------------------------------------------------------

def export_to_usd(blend_path: str, out_usd: str) -> None:
    """Open *blend_path* and export the scene to *out_usd*.

    Uses Blender's built-in ``wm.usd_export`` operator (no addon required
    since Blender 3.0; production-grade in 5.x).  Materials are emitted as
    UsdPreviewSurface networks; textures are copied next to the .usd file
    in a ``textures/`` subfolder so the resulting USD is self-contained.

    The resulting file is consumed by Maya 2027's ``mayaUsdPlugin`` which
    is shipped by default — no third-party plugin install required.
    """
    status("opening")
    bpy.ops.wm.open_mainfile(filepath=blend_path)
    progress(0.80, "opened blend")

    # Make sure everything is visible — export the whole scene.
    for obj in bpy.context.scene.objects:
        try:
            obj.hide_set(False)
        except Exception:
            pass
        obj.hide_render = False
        obj.hide_viewport = False

    status("exporting")
    out_dir = os.path.dirname(out_usd) or "."
    os.makedirs(out_dir, exist_ok=True)

    # The signature of ``wm.usd_export`` has changed across Blender versions
    # (e.g. ``export_textures`` was renamed/dropped, ``root_prim_path`` was
    # ``default_prim_path`` in 3.x).  Introspect the operator's rna_type
    # and pass only the kwargs the installed Blender actually understands.
    desired = dict(
        filepath=out_usd,
        selected_objects_only=False,
        visible_objects_only=True,
        export_animation=False,
        export_hair=False,
        export_uvmaps=True,
        export_normals=True,
        export_materials=True,
        export_textures=True,           # 4.1+: copy referenced images
        overwrite_textures=True,
        relative_paths=True,
        generate_preview_surface=True,  # write UsdPreviewSurface networks
        root_prim_path="/root",
        default_prim_path="/root",      # legacy name in Blender 3.x
        export_global_forward_selection="Y",
        export_global_up_selection="Z",
    )
    try:
        rna_props = set(bpy.ops.wm.usd_export.get_rna_type().properties.keys())
    except Exception as exc:  # noqa: BLE001
        log_line(f"usd_export: rna introspection failed ({exc}); using all kwargs")
        rna_props = set(desired.keys())

    accepted = {k: v for k, v in desired.items() if k in rna_props}
    log_line(f"usd_export: accepted kwargs = {sorted(accepted.keys())}")
    skipped = sorted(set(desired) - set(accepted))
    if skipped:
        log_line(f"usd_export: skipped unknown kwargs = {skipped}")

    bpy.ops.wm.usd_export(**accepted)
    progress(0.98, "exported usd")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args() -> dict:
    if "--" not in sys.argv:
        raise RuntimeError("No '--' separator found in argv.")
    args = sys.argv[sys.argv.index("--") + 1:]
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

    asset_data     = args.get("asset_data") or {}
    max_resolution = args.get("max_resolution", "2048")
    # Accept either ``out_usd`` (new) or ``out_glb`` (legacy) so we don't
    # break older Maya-side callers during the switchover.
    out_usd        = args.get("out_usd") or args.get("out_glb", "")
    api_key        = args.get("api_key", "")
    work_dir       = args.get("work_dir") or os.path.dirname(out_usd) or "."

    progress(0.0, "starting")

    try:
        url, ft = pick_download_url(asset_data, max_resolution)
        status(f"picked {ft}")
        log_line(f"picked file type={ft} url={url}")
    except Exception as exc:
        error(str(exc))
        return 1

    # The URL from search results points at /api/v1/download/<id>/ — we need
    # to exchange it for a signed CDN URL before fetching the bytes.
    scene_uuid = asset_data.get("sceneUuid") or asset_data.get("scene_uuid") or str(uuid.uuid4())
    try:
        signed_url = resolve_signed_url(url, api_key=api_key, scene_uuid=scene_uuid)
    except urllib.error.HTTPError as exc:
        error(f"HTTP {exc.code} while requesting signed URL: {exc.reason}")
        return 1
    except Exception as exc:
        error(f"signed URL request failed: {exc}")
        traceback.print_exc(file=sys.stdout)
        return 1

    blend_path = os.path.join(work_dir, f"_bk_dl_{asset_data.get('id', 'asset')}.blend")
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
        export_to_usd(blend_path, out_usd)
    except Exception as exc:
        error(f"export failed: {exc}")
        traceback.print_exc(file=sys.stdout)
        return 1

    progress(1.0, "done")
    done(out_usd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
