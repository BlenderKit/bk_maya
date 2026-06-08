"""Asset download orchestrator (Maya side).

Coordinates the whole pipeline:

1. Pick the right download URL / resolution from the asset's ``files`` list.
2. Spawn Blender headless via :class:`bk_maya.core.blender_runner.BlenderJob`
   to fetch the .blend and export it to a USD (.usd) file with
   UsdPreviewSurface materials + texture references.
3. Stream progress to the placement-locator gizmo (``downloadProgress``
   attribute) and update its enum ``downloadState`` along the way.
4. On success, import the .usd into the current Maya scene at the requested
   location / rotation and remove the gizmo.

Public entry point: :func:`download_asset`.
"""

from __future__ import annotations

import json
import logging
import math
import os
import platform
import re
from collections.abc import Sequence
from typing import Any

try:
    import maya.cmds as cmds  # type: ignore[import-not-found]
except ImportError:  # for unit tests outside Maya
    cmds = None  # type: ignore[assignment]

from . import auth
from .blender_runner import (
    MIN_BLENDER_MAJOR,
    BlenderJob,
    find_blender_executable,
    query_blender_version,
    version_meets_min,
)
from .prefs import prefs

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Active jobs registry (keeps QObjects alive)
# ---------------------------------------------------------------------------

_active_jobs: list[_DownloadController] = []


def _euler_from_normal_and_yaw(
    normal: Sequence[float],
    yaw_deg: float,
) -> tuple[float, float, float]:
    """Return XYZ Euler angles (degrees) that align local +Y to *normal*
    and then spin around the normal by *yaw_deg*.

    Mirrors :func:`bk_maya.plugins.placement_locator._local_to_world`
    so the imported asset lands in exactly the orientation the drag
    preview showed. Uses Rodrigues to build a rotation matrix and
    decomposes back to Maya's default XYZ Euler order.
    """
    nx, ny, nz = (float(v) for v in normal)
    n_len = math.sqrt(nx * nx + ny * ny + nz * nz) or 1.0
    nx, ny, nz = nx / n_len, ny / n_len, nz / n_len

    # Step 1: rotation around local Y (yaw).
    yr = math.radians(yaw_deg)
    cy_, sy_ = math.cos(yr), math.sin(yr)
    # M_yaw rows (column-major application: M*v where v is column).
    # M_yaw = | c  0  s |
    #         | 0  1  0 |
    #         |-s  0  c |
    yaw = (
        (cy_, 0.0, sy_),
        (0.0, 1.0, 0.0),
        (-sy_, 0.0, cy_),
    )

    # Step 2: Rodrigues align (0,1,0) -> normal.
    ax, az = nz, -nx
    sin2 = ax * ax + az * az
    if sin2 < 1e-12:
        if ny >= 0.0:
            align = (
                (1.0, 0.0, 0.0),
                (0.0, 1.0, 0.0),
                (0.0, 0.0, 1.0),
            )
        else:
            # 180° flip
            align = (
                (1.0, 0.0, 0.0),
                (0.0, -1.0, 0.0),
                (0.0, 0.0, -1.0),
            )
    else:
        sin_t = math.sqrt(sin2)
        cos_t = ny
        ax /= sin_t
        az /= sin_t
        c = cos_t
        s = sin_t
        omc = 1.0 - c
        # k = (ax, 0, az); standard Rodrigues:
        align = (
            (c + ax * ax * omc, -az * s, ax * az * omc),
            (az * s, c, -ax * s),
            (az * ax * omc, ax * s, c + az * az * omc),
        )

    # Combined = align @ yaw (apply yaw first, then align).
    def _matmul(a, b):
        return tuple(tuple(sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)) for i in range(3))

    m = _matmul(align, yaw)

    # Decompose to XYZ Euler (Maya default rotateOrder). Convention:
    # R = Rx * Ry * Rz applied to a column vector. Maya's xform rotation
    # input is interpreted in the node's rotateOrder, default XYZ, which
    # is exactly this composition for the SAME row-major matrix layout.
    # Stable extraction:
    sy = -m[2][0]
    if abs(sy) < 1.0 - 1e-7:
        ry = math.asin(sy)
        rx = math.atan2(m[2][1], m[2][2])
        rz = math.atan2(m[1][0], m[0][0])
    else:
        # Gimbal: ry = ±90°
        ry = math.copysign(math.pi / 2.0, sy)
        rx = math.atan2(-m[1][2], m[1][1])
        rz = 0.0
    return (math.degrees(rx), math.degrees(ry), math.degrees(rz))


# ---------------------------------------------------------------------------
# Controller
# ---------------------------------------------------------------------------

# Mirror of client/utils.go Slugify / GetAssetDirectoryName / PluralizeAssetType.
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(name: str) -> str:
    s = _SLUG_RE.sub("-", name.lower()).strip("-")
    return s[:50] if len(s) > 50 else s


def _pluralize_asset_type(t: str) -> str:
    return "brushes" if t == "brush" else f"{t}s"


# max_resolution pref → cache filename token (matches the Blender addon).
_RES_TOKEN = {
    "512": "0_5K",
    "1024": "1K",
    "2048": "2K",
    "4096": "4K",
    "8192": "8K",
}


def _resolution_key(max_res: str) -> str:
    return _RES_TOKEN.get(str(max_res), "")


def _client_app_id() -> int:
    try:
        from . import client_lib

        return client_lib.get_app_id()
    except Exception:
        return os.getpid()


def _client_addon_version() -> str:
    try:
        from . import client_lib

        return client_lib.ADDON_VERSION
    except Exception:
        return "0.0.0"


class _DownloadController:
    """One-shot controller for a single asset download."""

    def __init__(
        self,
        asset: dict[str, Any],
        location: Sequence[float],
        rotation_y: float,
        locator_name: str = "",
        surface_normal: Sequence[float] = (0.0, 1.0, 0.0),
    ) -> None:
        self.asset = asset
        self.location = tuple(location)
        self.rotation_y = float(rotation_y)
        self.surface_normal = tuple(surface_normal)
        self.locator_name = locator_name
        self.job = BlenderJob()
        self.work_dir = ""
        self.args_path = ""
        self.blend_path = ""
        self.out_usd = ""

    # ------------------------------------------------------------------
    def start(self) -> bool:
        exe = find_blender_executable()
        if not exe:
            msg = (
                "Blender executable not set. Open Settings → Files and choose "
                "your Blender application (Blender 5.0 or newer is required), "
                "then drag the asset again."
            )
            log.error("[BK download] %s", msg)
            self._cancel_action(msg)
            return False

        version = query_blender_version(exe)
        if version is None:
            msg = f"Could not determine Blender version of {exe!r}. Check the path in Settings → Files, then drag the asset again."
            log.error("[BK download] %s", msg)
            self._cancel_action(msg)
            return False
        if not version_meets_min(version):
            v = ".".join(str(x) for x in version)
            msg = (
                f"Blender {v} is too old. BlenderKit for Maya requires Blender "
                f"{MIN_BLENDER_MAJOR}.0 or newer. Update the path in Settings → Files, "
                "then drag the asset again."
            )
            log.error("[BK download] %s", msg)
            self._cancel_action(msg)
            return False
        log.info("[BK download] using Blender %s at %s", ".".join(str(x) for x in version), exe)

        # Use the *same* cache layout as the Go client (see
        # client/utils.go GetAssetDirectoryName / ServerToLocalFilename):
        #   <global_dir>/<assetType>s/<slug(name)[:16]>_<id>/
        #       <slug(name)>_<res>_<id>.blend
        # so a .blend already pulled in by a previous drag / by the Blender
        # addon is reused on the next drop and we never create a
        # "temp_downloads" sidecar folder.
        asset_id = str(self.asset.get("id") or self.asset.get("assetBaseId") or "asset")
        asset_name = str(self.asset.get("name") or self.asset.get("displayName") or "asset")
        asset_type = str(self.asset.get("assetType") or "model").lower()
        slug = _slugify(asset_name)
        dir_slug = slug[:16] if len(slug) > 16 else slug
        plural = _pluralize_asset_type(asset_type)
        self.work_dir = os.path.join(
            prefs.global_dir_resolved(),
            plural,
            f"{dir_slug}_{asset_id}",
        )
        os.makedirs(self.work_dir, exist_ok=True)

        res_key = _resolution_key(prefs.max_resolution)
        blend_name = f"{slug}_{res_key}_{asset_id}.blend" if res_key else f"{slug}_{asset_id}.blend"
        self.blend_path = os.path.join(self.work_dir, blend_name)
        self.out_usd = os.path.splitext(self.blend_path)[0] + ".usd"

        # Networking (signed URL + file download) is delegated to the local Go
        # client over loopback — direct HTTPS from headless Blender fails SSL
        # verification on macOS.  Pass the client's base URL + identifiers so
        # bg_download.py can call the blocking download wrappers.
        client_base_url = ""
        try:
            from . import client_lib

            client_lib.ensure_running()
            client_base_url = client_lib.get_base_url()
        except Exception as exc:
            log.warning("[BK download] could not reach Go client: %s", exc)

        args = {
            "asset_data": self.asset,
            "max_resolution": prefs.max_resolution,
            "blend_path": self.blend_path,
            "out_usd": self.out_usd,
            "api_key": auth.get_api_key(),
            "work_dir": self.work_dir,
            "client_base_url": client_base_url,
            "app_id": _client_app_id(),
            "addon_version": _client_addon_version(),
            "platform_version": platform.platform(),
        }
        self.args_path = os.path.join(self.work_dir, "args.json")
        with open(self.args_path, "w", encoding="utf-8") as fh:
            json.dump(args, fh)

        # Wire up signals
        self.job.progress.connect(self._on_progress)
        self.job.status.connect(self._on_status)
        self.job.finished.connect(self._on_finished)
        self.job.failed.connect(self._on_failed)
        self.job.log_line.connect(self._on_log_line)

        script_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "scripts",
            "bg_download.py",
        )
        self._set_locator_state("downloading")
        self._set_locator_progress(0.0)
        # Seed label: asset name + initial step.
        asset_name = str(self.asset.get("name") or self.asset.get("displayName") or "asset")
        self._set_locator_label(name=asset_name, status="Starting…")
        return self.job.start(script_path, [self.args_path], blender_exe=exe)

    # ------------------------------------------------------------------
    # Signal handlers
    # ------------------------------------------------------------------
    def _on_progress(self, frac: float, msg: str) -> None:
        log.debug("[BK download] %.0f%% %s", frac * 100, msg)
        self._set_locator_progress(frac)
        step = (msg or "Downloading").strip()
        self._set_locator_label(status=f"{step}: {int(round(frac * 100))}%")  # noqa: RUF046

    def _on_status(self, s: str) -> None:
        log.info("[BK download] %s", s)
        if s:
            self._set_locator_label(status=s.strip())

    def _on_log_line(self, line: str) -> None:
        log.debug("[BK blender] %s", line)

    def _on_failed(self, msg: str) -> None:
        log.error("[BK download] FAILED: %s", msg)
        self._set_locator_state("idle")
        self._delete_locator()
        self._cleanup()
        self._notify_ui(f"Download failed: {msg}")

    @staticmethod
    def _notify_ui(message: str, settings_tab: str | None = None) -> None:
        try:
            from ..ui.asset_bar import notify_error

            notify_error(message, settings_tab=settings_tab)
        except Exception:
            pass

    def _cancel_action(self, message: str) -> None:
        """Abort this drop cleanly: remove the placement gizmo, drop the job,
        and surface *message* with a shortcut to the Blender path setting.

        Used when the Blender executable is missing/invalid so the viewport
        isn't left with a stuck "downloading" gizmo.
        """
        self._set_locator_state("idle")
        self._delete_locator()
        self._cleanup()
        self._notify_ui(message, settings_tab="Files")

    def _on_finished(self, out_path: str) -> None:
        log.info("[BK download] usd ready: %s", out_path)
        # Surface the next stage in the gizmo label immediately so the user
        # sees "Importing…" instead of the gizmo appearing stuck on the
        # previous "Generating USD" message while mayaUsdImport blocks.
        self._set_locator_label(status="Importing…")
        self._set_locator_progress(1.0)

        # Force one viewport redraw NOW so the new label actually appears on
        # screen before mayaUsdImport blocks the main thread.  Without the
        # explicit refresh Maya batches the attribute change with the import
        # work and the user only ever sees the stale "Generating USD" text.
        if cmds is not None:
            try:
                cmds.refresh(force=True)
            except Exception as exc:
                log.debug("refresh failed: %s", exc)

        def _do_import() -> None:
            try:
                self._import_usd(out_path)
                self._set_locator_state("done")
                self._delete_locator()
            except Exception as exc:
                log.exception("[BK download] import failed: %s", exc)
                self._set_locator_state("idle")
            finally:
                self._cleanup()

        if cmds is not None:
            try:
                # Defer onto the idle loop so the QProcess.finished signal
                # returns cleanly and Maya gets one more redraw tick before
                # the blocking USD import takes over.
                cmds.evalDeferred(_do_import, lowestPriority=True)
                return
            except Exception as exc:
                log.debug("evalDeferred failed (%s); running inline", exc)
        _do_import()

    # ------------------------------------------------------------------
    # Maya integration
    # ------------------------------------------------------------------
    def _set_locator_state(self, state: str) -> None:
        if not (cmds and self.locator_name and cmds.objExists(self.locator_name)):
            return
        mapping = {"idle": 0, "downloading": 1, "done": 2}
        try:
            cmds.setAttr(f"{self.locator_name}.downloadState", mapping.get(state, 0))
        except Exception as exc:
            log.debug("setAttr downloadState failed: %s", exc)

    def _set_locator_progress(self, frac: float) -> None:
        if not (cmds and self.locator_name and cmds.objExists(self.locator_name)):
            return
        attr = f"{self.locator_name}.downloadProgress"
        try:
            if cmds.attributeQuery("downloadProgress", node=self.locator_name, exists=True):
                cmds.setAttr(attr, max(0.0, min(1.0, float(frac))))
        except Exception as exc:
            log.debug("setAttr downloadProgress failed: %s", exc)

    def _set_locator_label(self, name: str | None = None, status: str | None = None) -> None:
        if not self.locator_name:
            return
        try:
            from . import locator_state

            locator_state.set_label(self.locator_name, name=name, status=status)
        except Exception as exc:
            log.debug("set_label failed: %s", exc)

        # Mirror the label to Maya's viewport HUD so the user always sees the
        # current step, even when the MPxDrawOverride text path is suppressed
        # (off-screen, font fallback failed, draw override unloaded, etc.).
        if cmds is None:
            return
        entry = locator_state.get_label(self.locator_name)
        nm = entry.get("name") or ""
        st = entry.get("status") or ""
        if not (nm or st):
            return
        msg = f"<hl>{nm}</hl><br>{st}" if nm and st else (nm or st)
        try:
            cmds.inViewMessage(
                amg=msg,
                pos="topCenter",
                fade=False,
                clear="topCenter",
                fontSize=14,
            )
        except Exception as exc:
            log.debug("inViewMessage failed: %s", exc)

    def _delete_locator(self) -> None:
        # Always clear shared state first, even if the node is gone.
        try:
            from . import locator_state

            locator_state.clear_proxor_lines(self.locator_name or "")
            locator_state.clear_proxor_mesh(self.locator_name or "")
            locator_state.clear_label(self.locator_name or "")
        except Exception:
            pass
        # Clear any HUD message we put up for this download.
        if cmds is not None:
            try:
                cmds.inViewMessage(clear="topCenter")
            except Exception:
                pass
        if not (cmds and self.locator_name and cmds.objExists(self.locator_name)):
            return
        try:
            # The shape's transform is the parent — delete it cleanly.
            parents = cmds.listRelatives(self.locator_name, parent=True, fullPath=True) or []
            target = parents[0] if parents else self.locator_name
            cmds.delete(target)
        except Exception as exc:
            log.debug("delete locator failed: %s", exc)

    def _import_usd(self, usd_path: str) -> None:
        """Import the exported USD into the current Maya scene.

        Maya 2027 ships ``mayaUsdPlugin`` which registers the ``USD Import``
        translator and the ``mayaUSDImport`` command.  We prefer the
        dedicated command because it exposes ``shadingMode`` / material
        options directly; ``cmds.file`` is a fallback for unusual setups.
        """
        if not cmds:
            return
        if not os.path.isfile(usd_path):
            raise FileNotFoundError(usd_path)

        self._ensure_usd_plugin()

        before = set(cmds.ls(assemblies=True) or [])

        imported_via_command = False
        try:
            cmds.mayaUSDImport(
                file=usd_path,
                readAnimData=False,
                shadingMode=[("useRegistry", "UsdPreviewSurface")],
                preferredMaterial="standardSurface",
                importInstances=True,
            )
            imported_via_command = True
        except Exception as exc:
            log.debug("mayaUSDImport unavailable (%s); falling back to cmds.file", exc)

        if not imported_via_command:
            type_candidates = ["USD Import", "usdImport", "USD", None]
            last_exc: Exception | None = None
            for t in type_candidates:
                try:
                    kw = {
                        "i": True,
                        "ignoreVersion": True,
                        "mergeNamespacesOnClash": False,
                        "options": "",
                        "preserveReferences": True,
                    }
                    if t is not None:
                        kw["type"] = t
                    cmds.file(usd_path, **kw)
                    last_exc = None
                    break
                except RuntimeError as exc:
                    last_exc = exc
                    continue
            if last_exc is not None:
                raise RuntimeError(
                    "Could not import .usd — mayaUsdPlugin is not available. "
                    "Enable it via Windows → Settings/Preferences → "
                    f"Plug-in Manager. Last error: {last_exc}"
                )

        after = set(cmds.ls(assemblies=True) or [])
        new_roots = list(after - before)
        if not new_roots:
            log.warning("usd imported but no new top-level node detected.")
            return

        # Group imports under a single transform we can position.
        grp_name = "BK_" + re.sub(r"[^A-Za-z0-9_]", "_", self.asset.get("name", "asset"))
        grp = cmds.group(new_roots, name=grp_name)

        # Align the *bottom-centre* of the freshly-imported geometry's bbox
        # with the drop point. The placement preview (bbox + proxor) is drawn
        # with the same convention, so the final mesh lands exactly where
        # the user saw the cyan/green volume during the drag.
        # We do this BEFORE translating/rotating so the bbox is read in the
        # asset's native frame.
        try:
            bb = cmds.exactWorldBoundingBox(grp)  # [xmin, ymin, zmin, xmax, ymax, zmax]
            cx = (bb[0] + bb[3]) * 0.5
            cy = bb[1]
            cz = (bb[2] + bb[5]) * 0.5
        except Exception as exc:
            log.debug("exactWorldBoundingBox failed (%s); using origin pivot", exc)
            cx = cy = cz = 0.0

        x, y, z = self.location
        rx, ry, rz = _euler_from_normal_and_yaw(self.surface_normal, self.rotation_y)
        # Offset = drop point − asset bottom-centre.
        cmds.xform(grp, worldSpace=True, translation=(x - cx, y - cy, z - cz))
        # Rotate around the bottom-centre so the asset spins on its base.
        cmds.xform(grp, worldSpace=True, pivots=(x, y, z))
        cmds.xform(grp, worldSpace=True, rotation=(rx, ry, rz))

    @staticmethod
    def _ensure_usd_plugin() -> None:
        """Load ``mayaUsdPlugin`` if it isn't already."""
        if cmds is None:
            return
        plug = "mayaUsdPlugin"
        try:
            if cmds.pluginInfo(plug, query=True, loaded=True):
                return
        except Exception:
            pass
        try:
            cmds.loadPlugin(plug, quiet=True)
            if cmds.pluginInfo(plug, query=True, loaded=True):
                log.info("[BK download] loaded USD plugin: %s", plug)
                return
        except Exception as exc:
            log.warning(
                "[BK download] could not load mayaUsdPlugin (%s); USD import "
                "may fail. Enable it in the Plug-in Manager.",
                exc,
            )

    # ------------------------------------------------------------------
    def _cleanup(self) -> None:
        if self in _active_jobs:
            _active_jobs.remove(self)
        # Leave temp files for now — useful when debugging.  TODO: reap.


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def download_asset(
    asset: dict[str, Any],
    *,
    location: Sequence[float] = (0.0, 0.0, 0.0),
    rotation_y: float = 0.0,
    locator_name: str = "",
    surface_normal: Sequence[float] = (0.0, 1.0, 0.0),
) -> None:
    """Kick off an asynchronous download for *asset*.

    The function returns immediately; progress and completion are reported
    on the placement locator (if *locator_name* is given) and on the log.
    """
    if cmds is None:
        log.warning("download_asset called outside Maya — ignored.")
        return

    ctrl = _DownloadController(asset, location, rotation_y, locator_name, surface_normal=surface_normal)
    _active_jobs.append(ctrl)
    if not ctrl.start():  # noqa: SIM102
        # start() already logged + reset locator
        if ctrl in _active_jobs:
            _active_jobs.remove(ctrl)
